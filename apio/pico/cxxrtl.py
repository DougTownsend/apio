"""Generate Pico or host wrappers around a Yosys CXXRTL design model."""

import re
from dataclasses import dataclass
from typing import Dict, List


# -- A PCF pin number meaning that no physical GPIO is attached. On Pico it
# -- is driven by a 1 kHz timer; in host tests it toggles once per step.
INTERNAL_PIN = -1

_PORT_RE = re.compile(
    r"/\*(input|output|inout)\*/\s+(value|wire)<(\d+)>\s+"
    r"(p_[A-Za-z0-9_]+);"
)


class CxxrtlError(Exception):
    """Raised when a CXXRTL model cannot be wrapped for Pico GPIO."""


@dataclass(frozen=True)
class Port:
    """A public top-level port found in generated CXXRTL source."""

    name: str
    cxx_name: str
    direction: str
    storage: str
    width: int


@dataclass(frozen=True)
class ResolvedPort:
    """A top-level port with one PCF pin assignment per bit."""

    port: Port
    pins: List[int]


def _decode_cxxrtl_name(cxx_name: str) -> str:
    """Decode the identifier escaping used by Yosys's CXXRTL backend."""

    if not cxx_name.startswith("p_"):
        raise CxxrtlError(f"unexpected CXXRTL port name {cxx_name!r}")

    encoded = cxx_name[2:]
    result: List[str] = []
    index = 0
    while index < len(encoded):
        if encoded[index] != "_":
            result.append(encoded[index])
            index += 1
            continue
        if index + 1 < len(encoded) and encoded[index + 1] == "_":
            result.append("_")
            index += 2
            continue
        end = encoded.find("_", index + 1)
        if end < 0:
            raise CxxrtlError(
                f"cannot decode CXXRTL port name {cxx_name!r}"
            )
        try:
            result.append(chr(int(encoded[index + 1 : end], 16)))
        except ValueError as exc:
            raise CxxrtlError(
                f"cannot decode CXXRTL port name {cxx_name!r}"
            ) from exc
        index = end + 1
    return "".join(result)


def _cxxrtl_name(name: str) -> str:
    """Encode an RTLIL identifier as a public CXXRTL C++ identifier."""

    encoded = []
    for char in name.lstrip("\\"):
        if char == "_":
            encoded.append("__")
        elif char.isascii() and char.isalnum():
            encoded.append(char)
        else:
            encoded.append(f"_{ord(char):02x}_")
    return "p_" + "".join(encoded)


def parse_model_ports(model_source: str, top_module: str) -> List[Port]:
    """Extract public ports from a generated CXXRTL top-level struct."""

    struct_name = _cxxrtl_name(top_module)
    struct_match = re.search(
        rf"struct\s+{re.escape(struct_name)}\s*:\s*public\s+module\s*\{{"
        rf"(.*?)\}};\s*//\s*struct\s+{re.escape(struct_name)}",
        model_source,
        flags=re.DOTALL,
    )
    if not struct_match:
        raise CxxrtlError(
            f"CXXRTL output does not contain top module {top_module!r}"
        )

    ports = [
        Port(
            name=_decode_cxxrtl_name(match.group(4)),
            cxx_name=match.group(4),
            direction=match.group(1),
            storage=match.group(2),
            width=int(match.group(3)),
        )
        for match in _PORT_RE.finditer(struct_match.group(1))
    ]
    if not ports:
        raise CxxrtlError(
            f"CXXRTL top module {top_module!r} contains no public ports"
        )
    return ports


def resolve_ports(
    ports: List[Port], pin_map: Dict[str, int]
) -> List[ResolvedPort]:
    """Resolve and validate one PCF pin for every top-level port bit."""

    resolved: List[ResolvedPort] = []
    expected_keys = set()
    physical_pins: Dict[int, str] = {}

    for port in ports:
        if port.direction == "inout":
            raise CxxrtlError(
                f"top-level inout port {port.name!r} is not supported by "
                "the Pico GPIO wrapper"
            )

        pins = []
        for bit in range(port.width):
            key = port.name if port.width == 1 else f"{port.name}[{bit}]"
            expected_keys.add(key)
            if key not in pin_map:
                raise CxxrtlError(
                    f"no pin mapping for {key!r} -- add a "
                    f"'set_io {key} <gpio>' line to the .pcf file"
                )
            pin = pin_map[key]
            if pin != INTERNAL_PIN:
                if not 0 <= pin <= 29:
                    raise CxxrtlError(
                        f"pin mapping for {key!r} is {pin}; RP2040 GPIO "
                        "numbers must be 0 through 29, or -1 for an "
                        "internal signal"
                    )
                previous = physical_pins.get(pin)
                if previous is not None:
                    raise CxxrtlError(
                        f"GPIO {pin} is assigned to both {previous!r} and "
                        f"{key!r}"
                    )
                physical_pins[pin] = key
            pins.append(pin)
        resolved.append(ResolvedPort(port=port, pins=pins))

    unexpected = sorted(set(pin_map) - expected_keys)
    if unexpected:
        raise CxxrtlError(
            "PCF contains mappings for unknown top-level ports: "
            + ", ".join(repr(name) for name in unexpected)
        )
    return resolved


def _gpio_init_lines(ports: List[ResolvedPort]) -> List[str]:
    lines: List[str] = []
    for resolved in ports:
        direction = resolved.port.direction
        gpio_direction = "GPIO_IN" if direction == "input" else "GPIO_OUT"
        for pin in resolved.pins:
            if pin == INTERNAL_PIN:
                continue
            lines.append(f"  gpio_init({pin});")
            lines.append(f"  gpio_set_dir({pin}, {gpio_direction});")
    return lines


def _input_lines(ports: List[ResolvedPort], host: bool) -> List[str]:
    lines: List[str] = []
    for resolved in ports:
        if resolved.port.direction != "input":
            continue
        for bit, pin in enumerate(resolved.pins):
            if pin == INTERNAL_PIN:
                expression = "virtual_pin_state"
            elif host:
                expression = f"host_gpio[{pin}] & 1u"
            else:
                expression = f"gpio_get({pin}) & 1u"
            lines.append(
                f"  design.{resolved.port.cxx_name}.set_bit({bit}, "
                f"{expression});"
            )
    return lines


def _output_lines(ports: List[ResolvedPort], host: bool) -> List[str]:
    lines: List[str] = []
    for resolved in ports:
        if resolved.port.direction != "output":
            continue
        for bit, pin in enumerate(resolved.pins):
            if pin == INTERNAL_PIN:
                continue
            member = f"design.{resolved.port.cxx_name}"
            if resolved.port.storage == "wire":
                member += ".curr"
            value = f"{member}.bit({bit})"
            if host:
                lines.append(f"  host_gpio[{pin}] = {value};")
            else:
                lines.append(f"  gpio_put({pin}, {value});")
    return lines


def _pico_wrapper(
    ports: List[ResolvedPort], top_module: str, uses_internal_pin: bool
) -> str:
    struct_name = _cxxrtl_name(top_module)
    init_lines = _gpio_init_lines(ports)
    input_lines = _input_lines(ports, host=False)
    output_lines = _output_lines(ports, host=False)

    virtual_decl = ""
    virtual_init = ""
    if uses_internal_pin:
        virtual_decl = """\
static volatile uint8_t virtual_pin_state;

static bool virtual_clock_isr(struct repeating_timer *timer) {
  (void)timer;
  virtual_pin_state ^= 1;
  return true;
}

"""
        virtual_init = """\
  static struct repeating_timer virtual_clock_timer;
  add_repeating_timer_us(
      500, virtual_clock_isr, nullptr, &virtual_clock_timer);
"""

    body = [
        "",
        "#include <cstdint>",
        '#include "pico/bootrom.h"',
        '#include "pico/stdlib.h"',
        '#include "pico/time.h"',
        "",
        f"using cxxrtl_design::{struct_name};",
        "",
        virtual_decl.rstrip(),
        "int main() {",
        "  stdio_init_all();",
        *init_lines,
        virtual_init.rstrip(),
        f"  {struct_name} design;",
        "  design.step();",
        "  for (;;) {",
        *input_lines,
        "    design.step();",
        *output_lines,
        "    if (getchar_timeout_us(0) == 'b') {",
        "      reset_usb_boot(0, 0);",
        "    }",
        "  }",
        "}",
    ]
    return "\n".join(line for line in body if line is not None) + "\n"


def _host_wrapper(
    ports: List[ResolvedPort], top_module: str, uses_internal_pin: bool
) -> str:
    struct_name = _cxxrtl_name(top_module)
    input_lines = _input_lines(ports, host=True)
    output_lines = _output_lines(ports, host=True)
    body = [
        "",
        "#include <cstdint>",
        "static uint8_t host_gpio[256];",
        "static uint8_t virtual_pin_state;",
        f"static cxxrtl_design::{struct_name} design;",
        "",
        "static void step_once() {",
    ]
    if uses_internal_pin:
        body.append("  virtual_pin_state ^= 1;")
    body.extend(input_lines)
    body.append("  design.step();")
    body.extend(output_lines)
    body.extend(["}", ""])
    return "\n".join(body)


def generate_firmware(
    model_source: str,
    pin_map: Dict[str, int],
    top_module: str,
    *,
    target: str = "pico",
) -> str:
    """Append a PCF-driven Pico or host wrapper to a CXXRTL model."""

    if target not in ("pico", "host"):
        raise ValueError(f"unknown target {target!r}")

    ports = resolve_ports(parse_model_ports(model_source, top_module), pin_map)
    uses_internal_pin = any(
        pin == INTERNAL_PIN
        for resolved in ports
        if resolved.port.direction == "input"
        for pin in resolved.pins
    )
    wrapper = (
        _pico_wrapper(ports, top_module, uses_internal_pin)
        if target == "pico"
        else _host_wrapper(ports, top_module, uses_internal_pin)
    )
    return model_source.rstrip() + "\n" + wrapper
