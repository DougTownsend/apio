"""Test harness for apio/pico/codegen.py: synthesizes a small Verilog
module with the real yosys binary, generates C with target="host", links
it against a tiny C test-vector runner, compiles with the system C
compiler, and runs it -- so codegen correctness is checked against actual
compiled/executed behavior, not just generated source text.
"""

import json
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

from apio.pico.codegen import generate_c
from apio.pico.netlist import parse_yosys_json
from apio.pico.pcf import parse_pcf


def find_yosys() -> str:
    exe = shutil.which("yosys")
    if exe:
        return exe
    candidate = Path.home() / ".apio/packages/oss-cad-suite/bin/yosys"
    if candidate.exists():
        return str(candidate)
    return None


def find_cc() -> str:
    for exe in ("cc", "gcc", "clang"):
        found = shutil.which(exe)
        if found:
            return found
    return None


requires_yosys = pytest.mark.skipif(
    find_yosys() is None, reason="yosys not installed"
)
requires_cc = pytest.mark.skipif(find_cc() is None, reason="no C compiler")


# -- A test step: input pin values to set before step_once(), and the
# -- expected output pin values to check after it.
Step = Tuple[Dict[int, int], Dict[int, int]]


def run_case(
    tmp_path: Path,
    verilog_src: str,
    pcf_text: str,
    steps: List[Step],
) -> None:
    """Synthesizes verilog_src, generates host-target C, runs `steps` in
    order (each: set input pins, step_once(), assert output pins) inside
    a compiled native binary, raising AssertionError with the failing
    step index/values on mismatch."""

    v_path = tmp_path / "design.v"
    v_path.write_text(verilog_src, encoding="utf-8")

    json_path = tmp_path / "design.json"
    yosys = find_yosys()
    subprocess.run(
        [
            yosys,
            "-p",
            f"read_verilog {v_path}; proc; flatten; opt; "
            f"write_json {json_path}",
        ],
        check=True,
        capture_output=True,
    )

    pcf_path = tmp_path / "design.pcf"
    pcf_path.write_text(pcf_text, encoding="utf-8")

    netlist = parse_yosys_json(json_path)
    pin_map = parse_pcf(pcf_path)

    c_src = generate_c(netlist, pin_map, target="host", include_main=False)
    gen_c_path = tmp_path / "gen.c"
    gen_c_path.write_text(c_src, encoding="utf-8")

    harness_lines = [
        '#include "gen.c"',
        "#include <stdio.h>",
        "int main(void) {",
    ]
    for i, (inputs, _expected) in enumerate(steps):
        for pin, value in inputs.items():
            harness_lines.append(f"  host_gpio[{pin}] = {value};")
        harness_lines.append("  step_once();")
        harness_lines.append(f'  printf("STEP{i}");')
        pins_to_print = sorted({p for _i, exp in steps for p in exp})
        for pin in pins_to_print:
            harness_lines.append(f'  printf(" {pin}=%d", host_gpio[{pin}]);')
        harness_lines.append('  printf("\\n");')
    harness_lines.append("  return 0;")
    harness_lines.append("}")

    harness_path = tmp_path / "harness.c"
    harness_path.write_text("\n".join(harness_lines), encoding="utf-8")

    binary_path = tmp_path / "harness"
    subprocess.run(
        [find_cc(), "-std=c11", "-O0", "-o", str(binary_path), str(harness_path)],
        check=True,
        capture_output=True,
        cwd=tmp_path,
    )

    result = subprocess.run(
        [str(binary_path)], check=True, capture_output=True, text=True
    )
    out_lines = result.stdout.strip().splitlines()
    assert len(out_lines) == len(steps), (
        f"expected {len(steps)} output lines, got {len(out_lines)}: "
        f"{result.stdout!r}"
    )

    for i, (line, (_inputs, expected)) in enumerate(zip(out_lines, steps)):
        actual: Dict[int, int] = {}
        for token in line.split()[1:]:
            pin_str, val_str = token.split("=")
            actual[int(pin_str)] = int(val_str)
        for pin, expected_value in expected.items():
            assert actual[pin] == expected_value, (
                f"step {i}: pin {pin} expected {expected_value}, "
                f"got {actual[pin]} (full line: {line!r})"
            )
