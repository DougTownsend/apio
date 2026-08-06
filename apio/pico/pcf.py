"""Parser for the .pcf pin-constraint file format.

Apio's ice40 boards already use .pcf files (consumed directly by
nextpnr-ice40) to map a top-level Verilog port to a physical pin, with the
simple line format::

    set_io <port_name> <pin>
    set_io <port_name>[<bit_index>] <pin>

The pico architecture reuses this same file format and the same
apio.ini `constraint-file` mechanism, just reinterpreting `<pin>` as an
RP2040 GPIO number instead of an FPGA package pin name, and parsing it
here since nextpnr isn't in the pico build pipeline to parse it for us.

`<pin>` may also be apio.pico.codegen.INTERNAL_PIN (-1), for a signal
that shouldn't consume a real GPIO -- e.g. `set_io clk -1` gives a design
a free-running clock without spending one of the Pico's GPIOs on it. This
isn't specific to clk; see codegen.py for what INTERNAL_PIN does.
"""

import re
from pathlib import Path
from typing import Dict, Union


class PcfError(Exception):
    """Raised when a .pcf file can't be parsed."""


_LINE_RE = re.compile(r"^set_io\s+(\S+)\s+(\S+)$")


def parse_pcf(path: Union[str, Path]) -> Dict[str, int]:
    """Parses a .pcf file into a dict mapping port names (e.g. "led" or
    "leds[3]") to GPIO pin numbers."""

    path = Path(path)
    mapping: Dict[str, int] = {}

    with path.open(encoding="utf-8") as f:
        for lineno, raw_line in enumerate(f, start=1):
            line = raw_line.split("#", 1)[0].strip()
            if not line:
                continue

            match = _LINE_RE.match(line)
            if not match:
                raise PcfError(
                    f"{path}:{lineno}: cannot parse pcf line: {raw_line!r}"
                )

            name, pin_str = match.group(1), match.group(2)
            try:
                pin = int(pin_str)
            except ValueError as e:
                raise PcfError(
                    f"{path}:{lineno}: pico gpio pin must be an integer, "
                    f"got {pin_str!r}"
                ) from e

            if name in mapping:
                raise PcfError(
                    f"{path}:{lineno}: duplicate pin mapping for {name!r}"
                )
            mapping[name] = pin

    return mapping
