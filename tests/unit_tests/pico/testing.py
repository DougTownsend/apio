"""End-to-end test harness for Pico CXXRTL firmware generation.

Small Verilog designs are converted by the real Yosys CXXRTL backend,
wrapped for host GPIO, compiled as C++, and exercised with test vectors.
"""

import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

from apio.pico.cxxrtl import generate_firmware
from apio.pico.pcf import parse_pcf


def find_yosys() -> Optional[str]:
    exe = shutil.which("yosys")
    if exe:
        return exe
    candidate = Path.home() / ".apio/packages/oss-cad-suite/bin/yosys"
    if candidate.exists():
        return str(candidate)
    return None


def find_cxx() -> Optional[str]:
    for exe in ("c++", "g++", "clang++"):
        found = shutil.which(exe)
        if found:
            return found
    return None


def find_cxxrtl_runtime() -> Optional[Path]:
    """Finds the CXXRTL include root associated with the selected Yosys."""

    yosys = find_yosys()
    if yosys:
        yosys_config = Path(yosys).with_name("yosys-config")
        if yosys_config.exists():
            try:
                result = subprocess.run(
                    [str(yosys_config), "--datdir"],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                candidate = Path(result.stdout.strip())
            except subprocess.CalledProcessError:
                candidate = None
            if candidate:
                candidate /= "include/backends/cxxrtl/runtime"
                if (candidate / "cxxrtl" / "cxxrtl.h").exists():
                    return candidate

        candidate = (
            Path(yosys).resolve().parent.parent
            / "share/yosys/include/backends/cxxrtl/runtime"
        )
        if (candidate / "cxxrtl" / "cxxrtl.h").exists():
            return candidate
    return None


requires_yosys = pytest.mark.skipif(
    find_yosys() is None, reason="yosys not installed"
)
requires_cxx = pytest.mark.skipif(
    find_cxx() is None or find_cxxrtl_runtime() is None,
    reason="C++ compiler or CXXRTL runtime headers not installed",
)


# -- A test step: input pin values to set before step_once(), and the
# -- expected output pin values to check after it.
Step = Tuple[Dict[int, int], Dict[int, int]]


def run_case(
    tmp_path: Path,
    verilog_src: str,
    pcf_text: str,
    steps: List[Step],
) -> None:
    """Generates host-target CXXRTL, runs `steps` in
    order (each: set input pins, step_once(), assert output pins) inside
    a compiled native binary, raising AssertionError with the failing
    step index/values on mismatch."""

    v_path = tmp_path / "design.v"
    v_path.write_text(verilog_src, encoding="utf-8")

    model_path = tmp_path / "model.cc"
    yosys = find_yosys()
    assert yosys is not None
    subprocess.run(
        [
            yosys,
            "-p",
            f"read_verilog -sv {v_path}; prep -top main -flatten; "
            f"write_cxxrtl -O6 -g0 {model_path}",
        ],
        check=True,
        capture_output=True,
    )

    pcf_path = tmp_path / "design.pcf"
    pcf_path.write_text(pcf_text, encoding="utf-8")

    pin_map = parse_pcf(pcf_path)

    firmware_source = generate_firmware(
        model_path.read_text(encoding="utf-8"),
        pin_map,
        "main",
        target="host",
    )
    gen_path = tmp_path / "gen.cc"
    gen_path.write_text(firmware_source, encoding="utf-8")

    harness_lines = [
        '#include "gen.cc"',
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

    harness_path = tmp_path / "harness.cc"
    harness_path.write_text("\n".join(harness_lines), encoding="utf-8")

    binary_path = tmp_path / "harness"
    cxx = find_cxx()
    cxxrtl_runtime = find_cxxrtl_runtime()
    assert cxx is not None
    assert cxxrtl_runtime is not None
    subprocess.run(
        [
            cxx,
            "-std=c++17",
            "-O0",
            "-DCXXRTL_NDEBUG",
            "-I",
            str(cxxrtl_runtime),
            "-o",
            str(binary_path),
            str(harness_path),
        ],
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
