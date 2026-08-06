"""Parses a yosys generic-synthesis JSON netlist into a small IR used by
apio/pico/codegen.py.

The netlist must come from a *generic* yosys pass, not an FPGA-specific one:

    yosys -p "read_verilog -sv $SOURCES; proc; flatten; opt; write_json $TARGET"

`proc` lowers always blocks into cells, `flatten` inlines all submodule
instances so only yosys's fixed, documented internal cell types (`$and`,
`$mux`, `$sdff`, ...) remain -- no submodule-instance cells and no
FPGA-vendor primitives to special-case.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Union

# -- A netlist "bit" is either a net number, or a constant driver string
# -- as emitted by yosys ("0", "1", "x", "z").
Bit = Union[int, str]


class NetlistError(Exception):
    """Raised when a yosys JSON netlist can't be parsed or contains
    constructs the pico codegen doesn't support."""


@dataclass
class Port:
    name: str
    direction: str  # "input" | "output" | "inout"
    bits: List[Bit]


@dataclass
class Cell:
    name: str
    type: str
    parameters: Dict[str, str]
    port_directions: Dict[str, str]
    connections: Dict[str, List[Bit]] = field(default_factory=dict)

    def bits(self, port: str) -> List[Bit]:
        return self.connections[port]

    def bit(self, port: str) -> Bit:
        """Returns the single bit of a 1-bit port connection."""
        bits = self.connections[port]
        if len(bits) != 1:
            raise NetlistError(
                f"cell {self.name!r} port {port!r} expected 1 bit, "
                f"got {len(bits)}"
            )
        return bits[0]

    def param_int(self, name: str, default: int = 0) -> int:
        """Parses a yosys parameter value (a binary-digit string) as an
        unsigned int."""
        raw = self.parameters.get(name)
        if raw is None:
            return default
        return int(raw, 2)

    def param_bits(self, name: str, width: int) -> List[int]:
        """Parses a yosys parameter value (a binary-digit string,
        MSB-first) into a list of `width` bits, LSB-first."""
        raw = self.parameters.get(name, "0")
        value = int(raw, 2)
        return [(value >> i) & 1 for i in range(width)]


@dataclass
class Netlist:
    top_module: str
    ports: Dict[str, Port]
    cells: List[Cell]


def parse_yosys_json(path: Union[str, Path], top_module: str = None) -> Netlist:
    """Parses a yosys `write_json` output file into a Netlist."""

    path = Path(path)
    with path.open(encoding="utf-8") as f:
        data = json.load(f)

    modules = data.get("modules", {})
    if not modules:
        raise NetlistError(f"{path}: no modules found in yosys json")

    if top_module is None:
        if len(modules) != 1:
            raise NetlistError(
                f"{path}: multiple modules found ({sorted(modules)}) and no "
                "top_module specified -- did the yosys pass include "
                "'flatten'?"
            )
        top_module = next(iter(modules))

    if top_module not in modules:
        raise NetlistError(
            f"{path}: top module {top_module!r} not found "
            f"(have {sorted(modules)})"
        )

    module_json = modules[top_module]

    ports: Dict[str, Port] = {}
    for port_name, port_json in module_json.get("ports", {}).items():
        ports[port_name] = Port(
            name=port_name,
            direction=port_json["direction"],
            bits=list(port_json["bits"]),
        )

    cells: List[Cell] = []
    for cell_name, cell_json in module_json.get("cells", {}).items():
        cell_type = cell_json["type"]
        if not cell_type.startswith("$"):
            # -- A non-$ cell type means yosys left a submodule instance
            # -- (or FPGA primitive) un-flattened/un-lowered.
            raise NetlistError(
                f"{path}: cell {cell_name!r} has non-generic type "
                f"{cell_type!r} -- run yosys with 'proc; flatten; opt' "
                "before 'write_json'"
            )
        cells.append(
            Cell(
                name=cell_name,
                type=cell_type,
                parameters=cell_json.get("parameters", {}),
                port_directions=cell_json.get("port_directions", {}),
                connections=cell_json.get("connections", {}),
            )
        )

    return Netlist(top_module=top_module, ports=ports, cells=cells)
