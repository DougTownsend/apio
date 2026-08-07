"""Generates C source from a yosys generic netlist (apio/pico/netlist.py)
plus a .pcf pin mapping (apio/pico/pcf.py).

Semantics implemented (see pipico.md / the apio pico plan for the full
rationale): the generated firmware loops as fast as it can -- no fixed
rate, no artificial pacing. Each iteration reads all input pins, evaluates
all combinational logic, detects a rising edge on the design's single clk
net, commits register updates on that edge (or immediately for async
resets), re-evaluates combinational logic so outputs reflect the new
register state, and writes all output pins. This mirrors the "settle, then
commit" model of an event-driven Verilog simulator like Icarus Verilog,
just running the loop unthrottled instead of at a fixed rate: simple
designs iterate far faster than 1kHz, more complex ones (more cells to
evaluate per iteration) iterate slower.

Only a single clock net is supported (checked in build_module()) since the
loop can only sample one edge-detected input per iteration.

Any port bit can be mapped in the .pcf to INTERNAL_PIN (a negative,
not-a-real-GPIO sentinel) instead of a real pin number, to get a signal
without spending one of the Pico's comparatively scarce GPIOs on it. Since
the loop itself no longer runs at a fixed rate, INTERNAL_PIN can't just be
toggled once per iteration (see generate_c()) -- on the pico target it's
driven by a genuine 1kHz hardware timer interrupt instead
(_gen_pico_main()'s virtual_clock_isr()), independent of loop speed. This
isn't specific to clk or any other named signal (see resolve_pins()/
generate_c(), which handle it exactly like any other pin, just routed to
that timer-driven software source instead of gpio_get()/gpio_put()); it's
simply how a design gets a clock without spending a GPIO on it.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from apio.pico.netlist import Bit, Cell, Netlist, NetlistError

# -- A .pcf pin number reserved to mean "no real GPIO -- synthesize a
# -- value instead of reading hardware." Handled uniformly wherever a
# -- resolved pin is used below; not tied to any particular port name or
# -- role. On the pico target the value comes from a 1kHz hardware timer
# -- interrupt (see _gen_pico_main()); on the host target (no interrupt
# -- available) it's toggled once per step_once() call instead, for
# -- deterministic tests. Feeding that through the normal edge-detect
# -- pipeline is what makes it useful as a clock, for any design that
# -- maps its clk port to INTERNAL_PIN.
INTERNAL_PIN = -1

# -- Register (flip-flop) cell types. These are NOT part of the
# -- combinational dependency graph -- their Q output is a persistent
# -- "leaf" value for the rest of the design, updated only by
# -- commit_registers().
REGISTER_CELL_TYPES = {
    "$dff",
    "$dffe",
    "$adff",
    "$adffe",
    "$sdff",
    "$sdffe",
    "$sdffce",
}

# -- Cell types this codegen explicitly declines to support, with a
# -- specific reason surfaced to the user instead of a generic error.
UNSUPPORTED_CELL_TYPES = {
    "$mul": "multiplication",
    "$div": "division",
    "$mod": "modulo",
    "$pow": "exponentiation",
    "$mem": "memories",
    "$memrd": "memories",
    "$memwr": "memories",
    "$dlatch": "level-sensitive latches (use an always @(posedge clk) block)",
}


def _c_ident(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name)


def _expr(bit: Bit) -> str:
    """C expression reading a single netlist bit."""
    if bit == "0":
        return "0"
    if bit == "1":
        return "1"
    if bit in ("x", "z"):
        # -- Undefined-value bits: treated as 0. A teaching-tool
        # -- simplification -- flag designs that rely on 'x' propagation.
        return "0"
    return f"nets[{bit}]"


def _assign(bit: Bit, value_expr: str) -> str:
    if not isinstance(bit, int):
        raise NetlistError(f"cannot assign to constant net {bit!r}")
    return f"nets[{bit}] = ({value_expr});"


def _bit_at(bits: List[Bit], i: int, signed: bool) -> Bit:
    if i < len(bits):
        return bits[i]
    if not bits:
        return "0"
    return bits[-1] if signed else "0"


def _pack_expr(bits: List[Bit], signed: bool) -> str:
    if len(bits) > 32:
        raise NetlistError(
            f"signal is {len(bits)} bits wide; this codegen supports at "
            "most 32 bits for arithmetic/comparison operations"
        )
    if not bits:
        return "0"
    terms = " | ".join(f"(({_expr(b)}) << {i})" for i, b in enumerate(bits))
    packed = f"({terms})"
    if signed:
        width = len(bits)
        shift = 32 - width
        return f"((int32_t)(({packed}) << {shift}) >> {shift})"
    return packed


def _reduce_bool_expr(bits: List[Bit]) -> str:
    if not bits:
        return "0"
    ored = " | ".join(f"({_expr(b)})" for b in bits)
    return f"(({ored}) != 0)"


# ---------------------------------------------------------------------
# -- Per-cell-type combinational codegen.
# ---------------------------------------------------------------------


def _gen_bitwise_unary(cell: Cell, op: str) -> List[str]:
    a = cell.bits("A")
    y = cell.bits("Y")
    a_signed = bool(cell.param_int("A_SIGNED"))
    lines = []
    for i, ybit in enumerate(y):
        aexpr = _expr(_bit_at(a, i, a_signed))
        val = aexpr if op == "pos" else f"!({aexpr})"
        lines.append(_assign(ybit, val))
    return lines


def _gen_bitwise_binary(cell: Cell, c_op: str, negate: bool = False) -> List[str]:
    a = cell.bits("A")
    b = cell.bits("B")
    y = cell.bits("Y")
    a_signed = bool(cell.param_int("A_SIGNED"))
    b_signed = bool(cell.param_int("B_SIGNED"))
    lines = []
    for i, ybit in enumerate(y):
        aexpr = _expr(_bit_at(a, i, a_signed))
        bexpr = _expr(_bit_at(b, i, b_signed))
        val = f"(({aexpr}) {c_op} ({bexpr}))"
        if negate:
            val = f"!{val}"
        lines.append(_assign(ybit, val))
    return lines


def _gen_mux(cell: Cell) -> List[str]:
    a = cell.bits("A")
    b = cell.bits("B")
    s = cell.bit("S")
    y = cell.bits("Y")
    sexpr = _expr(s)
    lines = []
    for i, ybit in enumerate(y):
        aexpr = _expr(_bit_at(a, i, False))
        bexpr = _expr(_bit_at(b, i, False))
        lines.append(_assign(ybit, f"(({sexpr}) ? ({bexpr}) : ({aexpr}))"))
    return lines


def _gen_pmux(cell: Cell) -> List[str]:
    a = cell.bits("A")
    b = cell.bits("B")
    s = cell.bits("S")
    y = cell.bits("Y")
    width = len(y)
    nsel = len(s)
    lines = []
    for i, ybit in enumerate(y):
        chain = _expr(_bit_at(a, i, False))
        for k in range(nsel):
            bexpr = _expr(b[k * width + i])
            chain = f"(({_expr(s[k])}) ? ({bexpr}) : ({chain}))"
        lines.append(_assign(ybit, chain))
    return lines


def _gen_logic_binop(cell: Cell, c_op: str) -> List[str]:
    a_bool = _reduce_bool_expr(cell.bits("A"))
    b_bool = _reduce_bool_expr(cell.bits("B"))
    y = cell.bit("Y")
    return [_assign(y, f"((({a_bool}) {c_op} ({b_bool})) ? 1 : 0)")]


def _gen_logic_not(cell: Cell) -> List[str]:
    a_bool = _reduce_bool_expr(cell.bits("A"))
    y = cell.bit("Y")
    return [_assign(y, f"!({a_bool})")]


def _gen_reduce_and(cell: Cell) -> List[str]:
    bits = cell.bits("A")
    y = cell.bit("Y")
    anded = " & ".join(f"({_expr(b)})" for b in bits) if bits else "1"
    return [_assign(y, f"(({anded}) ? 1 : 0)")]


def _gen_reduce_or_bool(cell: Cell) -> List[str]:
    y = cell.bit("Y")
    return [_assign(y, f"({_reduce_bool_expr(cell.bits('A'))} ? 1 : 0)")]


def _gen_reduce_xor(cell: Cell, negate: bool = False) -> List[str]:
    bits = cell.bits("A")
    y = cell.bit("Y")
    xored = " ^ ".join(f"({_expr(b)})" for b in bits) if bits else "0"
    val = f"(({xored}) & 1)"
    if negate:
        val = f"!{val}"
    return [_assign(y, val)]


def _gen_compare(cell: Cell, c_op: str) -> List[str]:
    a_signed = bool(cell.param_int("A_SIGNED"))
    b_signed = bool(cell.param_int("B_SIGNED"))
    a_pack = _pack_expr(cell.bits("A"), a_signed)
    b_pack = _pack_expr(cell.bits("B"), b_signed)
    y = cell.bit("Y")
    return [_assign(y, f"((({a_pack}) {c_op} ({b_pack})) ? 1 : 0)")]


def _gen_arith_binary(cell: Cell, c_op: str) -> List[str]:
    a_signed = bool(cell.param_int("A_SIGNED"))
    b_signed = bool(cell.param_int("B_SIGNED"))
    a_pack = _pack_expr(cell.bits("A"), a_signed)
    b_pack = _pack_expr(cell.bits("B"), b_signed)
    y = cell.bits("Y")
    tmp = f"__t_{_c_ident(cell.name)}"
    lines = [f"{{ int32_t {tmp} = ({a_pack}) {c_op} ({b_pack});"]
    for i, ybit in enumerate(y):
        lines.append(f"  {_assign(ybit, f'(({tmp}) >> {i}) & 1')}")
    lines.append("}")
    return lines


def _gen_neg(cell: Cell) -> List[str]:
    a_signed = bool(cell.param_int("A_SIGNED"))
    a_pack = _pack_expr(cell.bits("A"), a_signed)
    y = cell.bits("Y")
    tmp = f"__t_{_c_ident(cell.name)}"
    lines = [f"{{ int32_t {tmp} = -({a_pack});"]
    for i, ybit in enumerate(y):
        lines.append(f"  {_assign(ybit, f'(({tmp}) >> {i}) & 1')}")
    lines.append("}")
    return lines


def _gen_shift(cell: Cell, left: bool, arithmetic: bool) -> List[str]:
    a_signed = bool(cell.param_int("A_SIGNED")) if arithmetic else False
    a_pack = _pack_expr(cell.bits("A"), a_signed)
    b_pack = _pack_expr(cell.bits("B"), False)
    y = cell.bits("Y")
    cast = "int32_t" if arithmetic else "uint32_t"
    op = "<<" if left else ">>"
    tmp = f"__t_{_c_ident(cell.name)}"
    lines = [f"{{ {cast} {tmp} = (({cast})({a_pack})) {op} ({b_pack});"]
    for i, ybit in enumerate(y):
        lines.append(f"  {_assign(ybit, f'(({tmp}) >> {i}) & 1')}")
    lines.append("}")
    return lines


_COMB_HANDLERS = {
    "$not": lambda c: _gen_bitwise_unary(c, "not"),
    "$pos": lambda c: _gen_bitwise_unary(c, "pos"),
    "$and": lambda c: _gen_bitwise_binary(c, "&"),
    "$or": lambda c: _gen_bitwise_binary(c, "|"),
    "$xor": lambda c: _gen_bitwise_binary(c, "^"),
    "$xnor": lambda c: _gen_bitwise_binary(c, "^", negate=True),
    "$mux": _gen_mux,
    "$pmux": _gen_pmux,
    "$logic_and": lambda c: _gen_logic_binop(c, "&&"),
    "$logic_or": lambda c: _gen_logic_binop(c, "||"),
    "$logic_not": _gen_logic_not,
    "$reduce_and": _gen_reduce_and,
    "$reduce_or": _gen_reduce_or_bool,
    "$reduce_bool": _gen_reduce_or_bool,
    "$reduce_xor": lambda c: _gen_reduce_xor(c),
    "$reduce_xnor": lambda c: _gen_reduce_xor(c, negate=True),
    "$eq": lambda c: _gen_compare(c, "=="),
    "$ne": lambda c: _gen_compare(c, "!="),
    "$lt": lambda c: _gen_compare(c, "<"),
    "$le": lambda c: _gen_compare(c, "<="),
    "$gt": lambda c: _gen_compare(c, ">"),
    "$ge": lambda c: _gen_compare(c, ">="),
    "$add": lambda c: _gen_arith_binary(c, "+"),
    "$sub": lambda c: _gen_arith_binary(c, "-"),
    "$neg": _gen_neg,
    "$shl": lambda c: _gen_shift(c, left=True, arithmetic=False),
    "$sshl": lambda c: _gen_shift(c, left=True, arithmetic=True),
    "$shr": lambda c: _gen_shift(c, left=False, arithmetic=False),
    "$sshr": lambda c: _gen_shift(c, left=False, arithmetic=True),
}


def _wrap_if(
    cond: str, then_lines: List[str], else_lines: Optional[List[str]] = None
) -> List[str]:
    lines = [f"if ({cond}) {{"]
    lines += [f"  {l}" for l in then_lines]
    if else_lines is not None:
        lines.append("} else {")
        lines += [f"  {l}" for l in else_lines]
    lines.append("}")
    return lines


def _gen_register(cell: Cell) -> List[str]:
    """Generates the commit_registers() body for one flip-flop cell.

    Composed bottom-up: D-latch -> (optionally EN-gated) -> (optionally
    SRST-gated, sync) -> gated by 'edge' -> (optionally ARST-gated, async,
    checked every call regardless of 'edge').
    """
    d = cell.bits("D")
    q = cell.bits("Q")
    width = len(q)

    latch_lines = [_assign(qbit, _expr(dbit)) for qbit, dbit in zip(q, d)]

    if "EN" in cell.connections:
        en_pol = cell.param_int("EN_POLARITY", 1)
        en_expr = _expr(cell.bit("EN"))
        cond = en_expr if en_pol else f"!({en_expr})"
        latch_lines = _wrap_if(cond, latch_lines)

    if "SRST" in cell.connections:
        srst_pol = cell.param_int("SRST_POLARITY", 1)
        srst_val = cell.param_bits("SRST_VALUE", width)
        srst_expr = _expr(cell.bit("SRST"))
        cond = srst_expr if srst_pol else f"!({srst_expr})"
        reset_lines = [_assign(qbit, str(v)) for qbit, v in zip(q, srst_val)]
        latch_lines = _wrap_if(cond, reset_lines, latch_lines)

    edge_lines = _wrap_if("edge", latch_lines)

    if "ARST" in cell.connections:
        arst_pol = cell.param_int("ARST_POLARITY", 1)
        arst_val = cell.param_bits("ARST_VALUE", width)
        arst_expr = _expr(cell.bit("ARST"))
        cond = arst_expr if arst_pol else f"!({arst_expr})"
        reset_lines = [_assign(qbit, str(v)) for qbit, v in zip(q, arst_val)]
        return _wrap_if(cond, reset_lines, edge_lines)

    return edge_lines


# ---------------------------------------------------------------------
# -- Dependency graph / topological sort.
# ---------------------------------------------------------------------


@dataclass
class BuiltModule:
    comb_cells_in_order: List[Cell]
    register_cells: List[Cell]
    clk_bit: int
    clk_polarity: int
    max_net: int


def _input_bits_of(cell: Cell) -> List[Bit]:
    bits: List[Bit] = []
    for port, direction in cell.port_directions.items():
        if direction in ("input", "inout"):
            bits.extend(cell.connections.get(port, []))
    return bits


def _output_bits_of(cell: Cell) -> List[Bit]:
    bits: List[Bit] = []
    for port, direction in cell.port_directions.items():
        if direction in ("output", "inout"):
            bits.extend(cell.connections.get(port, []))
    return bits


def build_module(netlist: Netlist) -> BuiltModule:
    comb_cells: List[Cell] = []
    register_cells: List[Cell] = []

    for cell in netlist.cells:
        if cell.type in REGISTER_CELL_TYPES:
            register_cells.append(cell)
        elif cell.type in UNSUPPORTED_CELL_TYPES:
            reason = UNSUPPORTED_CELL_TYPES[cell.type]
            raise NetlistError(
                f"cell {cell.name!r} uses unsupported construct "
                f"({reason}, type {cell.type}) -- not supported by the "
                "pico interpreter codegen"
            )
        elif cell.type in _COMB_HANDLERS:
            comb_cells.append(cell)
        else:
            raise NetlistError(
                f"cell {cell.name!r} has unsupported cell type {cell.type!r}"
            )

    # -- Single shared clock check.
    clk_signatures = set()
    for cell in register_cells:
        clk_bit = cell.bit("CLK")
        if not isinstance(clk_bit, int):
            raise NetlistError(
                f"cell {cell.name!r}: clk must be a real net, not a "
                f"constant ({clk_bit!r})"
            )
        clk_pol = cell.param_int("CLK_POLARITY", 1)
        clk_signatures.add((clk_bit, clk_pol))
    if len(clk_signatures) > 1:
        raise NetlistError(
            "design uses more than one clock net/polarity "
            f"({clk_signatures}) -- this interpreter only supports a "
            "single shared clk sampled once per loop iteration"
        )
    clk_bit, clk_polarity = next(iter(clk_signatures)) if clk_signatures else (
        None,
        1,
    )

    # -- net_driver: combinational output bit -> driving cell.
    net_driver: Dict[int, Cell] = {}
    for cell in comb_cells:
        for bit in _output_bits_of(cell):
            if isinstance(bit, int):
                if bit in net_driver:
                    raise NetlistError(f"net {bit} driven by multiple cells")
                net_driver[bit] = cell

    # -- Kahn's algorithm topological sort over comb cells.
    deps: Dict[str, set] = {}
    dependents: Dict[str, List[str]] = {c.name: [] for c in comb_cells}
    cells_by_name = {c.name: c for c in comb_cells}
    indegree: Dict[str, int] = {}

    for cell in comb_cells:
        cell_deps = set()
        for bit in _input_bits_of(cell):
            if isinstance(bit, int) and bit in net_driver:
                driver = net_driver[bit]
                if driver.name != cell.name:
                    cell_deps.add(driver.name)
        deps[cell.name] = cell_deps
        indegree[cell.name] = len(cell_deps)
        for dep_name in cell_deps:
            dependents[dep_name].append(cell.name)

    ready = [name for name, deg in indegree.items() if deg == 0]
    ready.sort()  # -- deterministic output ordering
    ordered_names: List[str] = []
    while ready:
        name = ready.pop(0)
        ordered_names.append(name)
        for dependent_name in sorted(dependents[name]):
            indegree[dependent_name] -= 1
            if indegree[dependent_name] == 0:
                ready.append(dependent_name)
        ready.sort()

    if len(ordered_names) != len(comb_cells):
        remaining = set(cells_by_name) - set(ordered_names)
        raise NetlistError(
            "design contains a combinational loop (not supported by this "
            f"straight-line interpreter): cells {sorted(remaining)}"
        )

    ordered_cells = [cells_by_name[n] for n in ordered_names]

    max_net = 0
    for cell in netlist.cells:
        for bits in cell.connections.values():
            for bit in bits:
                if isinstance(bit, int):
                    max_net = max(max_net, bit)
    for port in netlist.ports.values():
        for bit in port.bits:
            if isinstance(bit, int):
                max_net = max(max_net, bit)

    return BuiltModule(
        comb_cells_in_order=ordered_cells,
        register_cells=register_cells,
        clk_bit=clk_bit,
        clk_polarity=clk_polarity,
        max_net=max_net,
    )


# ---------------------------------------------------------------------
# -- Pin mapping.
# ---------------------------------------------------------------------


def resolve_pins(
    netlist: Netlist, pin_map: Dict[str, int]
) -> List[Tuple[int, int, str]]:
    """Resolves each top-level port bit to a GPIO pin.

    Returns a list of (net_bit, gpio_pin, direction) tuples.
    """
    resolved: List[Tuple[int, int, str]] = []
    for port in netlist.ports.values():
        width = len(port.bits)
        for i, bit in enumerate(port.bits):
            if not isinstance(bit, int):
                continue
            key = f"{port.name}[{i}]" if width > 1 else port.name
            if key not in pin_map:
                raise NetlistError(
                    f"no pin mapping for {key!r} -- add a "
                    f"'set_io {key} <gpio>' line to the .pcf file"
                )
            resolved.append((bit, pin_map[key], port.direction))
    return resolved


# ---------------------------------------------------------------------
# -- C source assembly.
# ---------------------------------------------------------------------

_HOST_PRELUDE = """\
/* Host-simulation build: no pico-sdk, no real GPIO. Tests drive
 * host_gpio[] directly and call step_once() to exercise the generated
 * logic on the development machine before it ever touches hardware. */
#include <stdint.h>
#include <string.h>

static uint8_t host_gpio[256];

static inline int gpio_get(int pin) { return host_gpio[pin] & 1; }
static inline void gpio_put(int pin, int value) { host_gpio[pin] = value & 1; }
"""

_PICO_PRELUDE = """\
#include <stdint.h>
#include <stdbool.h>
#include "pico/stdlib.h"
#include "pico/bootrom.h"
#include "pico/time.h"
"""

# -- Half-period of the INTERNAL_PIN virtual clock, in microseconds.
# -- 500us -> a hardware-timer interrupt toggles it into a 1kHz square
# -- wave (1000 rising edges/sec), independent of how fast the main loop
# -- itself runs (which is unthrottled -- see _gen_pico_main()).
_VIRTUAL_CLOCK_HALF_PERIOD_US = 500


def generate_c(
    netlist: Netlist,
    pin_map: Dict[str, int],
    target: str = "pico",
    include_main: bool = True,
) -> str:
    """Generates a complete C source file interpreting `netlist` in an
    unthrottled loop (speed depends on design complexity), reading/writing
    GPIOs per `pin_map` (parsed from a .pcf file by apio.pico.pcf.parse_pcf).

    `target` is "pico" (pico-sdk build) or "host" (native build for
    unit testing without hardware).
    """
    if target not in ("pico", "host"):
        raise ValueError(f"unknown target {target!r}")

    module = build_module(netlist)
    pins = resolve_pins(netlist, pin_map)
    uses_internal_pin = any(pin == INTERNAL_PIN for _bit, pin, _dir in pins)

    lines: List[str] = []
    lines.append("/* Auto-generated by apio pico codegen. Do not edit. */")
    lines.append(_PICO_PRELUDE if target == "pico" else _HOST_PRELUDE)
    lines.append(f"static uint8_t nets[{module.max_net + 1}];")

    # -- Explicit reset of every net to a known value at startup.
    # --
    # -- Static storage is already zeroed before main() by the C runtime,
    # -- so for a design whose registers all start at 0 this is redundant
    # -- in principle. It is emitted anyway, for two reasons. It is the
    # -- only thing that applies a *non-zero* Verilog initial value (`reg
    # -- x = 1'b1;`), which was previously discarded outright -- yosys
    # -- records it, but nothing downstream read it. And it makes the
    # -- reset explicit in the generated code rather than an inherited
    # -- property of where the linker happened to place an array, so
    # -- start-up state is the same no matter how the firmware was
    # -- entered.
    init_lines = [
        f"  nets[{bit}] = {value};"
        for bit, value in sorted(netlist.net_inits.items())
        if bit <= module.max_net
    ]
    # -- Clock-edge detector state. File scope rather than a static local
    # -- inside step_once() so reset_state() can clear it too: leaving it
    # -- behind would make the first iteration after a reset see a stale
    # -- edge and clock the registers once spuriously.
    lines.append("static int prev_clk;")
    lines.append("static void reset_state(void) {")
    lines.append("  for (unsigned i = 0; i < sizeof(nets); i++) nets[i] = 0;")
    lines.append("  prev_clk = 0;")
    lines.extend(init_lines)
    lines.append("}")

    if uses_internal_pin:
        if target == "pico":
            # -- Written from a hardware timer interrupt (see
            # -- _gen_pico_main()), read from the main loop: volatile
            # -- because of that cross-context access, but a single-byte
            # -- read/write is naturally atomic on this core so no lock
            # -- is needed.
            lines.append(
                "static volatile uint8_t virtual_pin_state = 0; "
                "/* toggled by a 1kHz hw timer irq; backs INTERNAL_PIN "
                "reads */"
            )
        else:
            # -- Host target has no timer interrupt: toggled once per
            # -- step_once() call instead, so tests stay deterministic
            # -- and don't depend on wall-clock time.
            lines.append(
                "static uint8_t virtual_pin_state = 0; "
                "/* host target: toggled once per step_once() call */"
            )
    lines.append("")

    # -- read_inputs() / write_outputs()
    lines.append("static void read_inputs(void) {")
    for bit, pin, direction in pins:
        if direction not in ("input", "inout"):
            continue
        if pin == INTERNAL_PIN:
            lines.append(f"  nets[{bit}] = virtual_pin_state;")
        else:
            lines.append(f"  nets[{bit}] = gpio_get({pin}) & 1;")
    lines.append("}")
    lines.append("")

    lines.append("static void write_outputs(void) {")
    for bit, pin, direction in pins:
        if direction not in ("output", "inout"):
            continue
        if pin == INTERNAL_PIN:
            continue  # -- no real pin to write to.
        lines.append(f"  gpio_put({pin}, nets[{bit}]);")
    lines.append("}")
    lines.append("")

    # -- eval_comb()
    lines.append("static void eval_comb(void) {")
    for cell in module.comb_cells_in_order:
        handler = _COMB_HANDLERS[cell.type]
        for line in handler(cell):
            lines.append(f"  {line}")
    lines.append("}")
    lines.append("")

    # -- commit_registers()
    lines.append("static void commit_registers(int edge) {")
    for cell in module.register_cells:
        for line in _gen_register(cell):
            lines.append(f"  {line}")
    lines.append("}")
    lines.append("")

    # -- step_once(): one loop iteration.
    clk_expr = _expr(module.clk_bit) if module.clk_bit is not None else "0"
    clk_active = "" if module.clk_polarity else "!"
    lines.append("static void step_once(void) {")
    if uses_internal_pin and target == "host":
        lines.append("  virtual_pin_state ^= 1;")
    lines.append("  read_inputs();")
    lines.append("  eval_comb();")
    lines.append(f"  int clk_now = {clk_active}({clk_expr}) ? 1 : 0;")
    lines.append("  int edge = clk_now && !prev_clk;")
    lines.append("  prev_clk = clk_now;")
    lines.append("  commit_registers(edge);")
    lines.append("  eval_comb();")
    lines.append("  write_outputs();")
    lines.append("}")
    lines.append("")

    if include_main and target == "pico":
        lines.append(_gen_pico_main(pins, uses_internal_pin))
    elif include_main and target == "host":
        lines.append(
            "int main(void) { reset_state(); for (;;) { step_once(); } "
            "return 0; }"
        )

    return "\n".join(lines) + "\n"


def _gen_pico_main(
    pins: List[Tuple[int, int, str]], uses_internal_pin: bool
) -> str:
    init_lines = []
    seen_pins = set()
    for _bit, pin, direction in pins:
        if pin == INTERNAL_PIN or pin in seen_pins:
            continue
        seen_pins.add(pin)
        init_lines.append(f"  gpio_init({pin});")
        if direction in ("output", "inout"):
            init_lines.append(f"  gpio_set_dir({pin}, GPIO_OUT);")
        else:
            init_lines.append(f"  gpio_set_dir({pin}, GPIO_IN);")
    init_block = "\n".join(init_lines)

    virtual_clock_decl = ""
    virtual_clock_init = ""
    if uses_internal_pin:
        virtual_clock_decl = """\
/* Toggles the INTERNAL_PIN virtual clock at a fixed 1kHz rate, driven by
 * a hardware timer interrupt so it stays accurate regardless of how fast
 * (or slow, for a complex design) the unthrottled main loop below runs. */
static bool virtual_clock_isr(struct repeating_timer *t) {
  (void)t;
  virtual_pin_state ^= 1;
  return true;
}

"""
        virtual_clock_init = f"""\
  static struct repeating_timer virtual_clock_timer;
  add_repeating_timer_us(
      {_VIRTUAL_CLOCK_HALF_PERIOD_US}, virtual_clock_isr, NULL,
      &virtual_clock_timer);
"""

    return f"""\
{virtual_clock_decl}\
/* If a byte 'b' (ASCII 'b' for bootloader) arrives on stdin (USB-serial),
 * reboot into the USB mass-storage bootloader so apio upload can flash
 * new firmware without a physical BOOTSEL button press. */
static void check_bootloader_trigger(void) {{
  int c = getchar_timeout_us(0);
  if (c == 'b') {{
    reset_usb_boot(0, 0);
  }}
}}

int main(void) {{
  stdio_init_all();
  /* Put every register at its Verilog initial value before anything
   * runs, so a freshly flashed board always starts from the same state
   * regardless of how it got here. */
  reset_state();
{init_block}
{virtual_clock_init}\
  /* Unthrottled: runs as fast as the generated logic computes. Simple
   * designs loop far faster than 1kHz; more complex ones take longer
   * per iteration and loop slower -- there's no artificial pacing here. */
  for (;;) {{
    check_bootloader_trigger();
    step_once();
  }}
  return 0;
}}
"""
