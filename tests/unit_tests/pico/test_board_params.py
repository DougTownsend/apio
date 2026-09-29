"""Unit tests of the Pico port resolution, board parameters, and upload
volume selection. Unlike test_codegen.py these need no Yosys or C++
compiler."""

from pathlib import Path

import pytest

from apio.pico import runtime, upload
from apio.pico.cxxrtl import (
    INTERNAL_PIN,
    CxxrtlError,
    generate_firmware,
    resolve_ports,
    parse_model_ports,
)

# -- A minimal stand-in for Yosys CXXRTL output, with just enough of the
# -- top-level struct for parse_model_ports() to find its ports.
_MODEL = """
struct p_main : public module {
  /*input*/ value<1> p_clk;
  /*input*/ value<1> p_a;
  /*output*/ wire<1> p_y;
}; // struct p_main
"""


def test_unused_pcf_entries_warn_instead_of_failing():
    """PCF entries matching no top-level port, whether mapped to a real
    GPIO or to INTERNAL_PIN, are reported through `warn` and otherwise
    ignored, as nextpnr-ice40 does for an upduino."""

    pin_map = {
        "clk": INTERNAL_PIN,
        "a": 0,
        "y": 1,
        "unused_clk": INTERNAL_PIN,
        "unused_led": 25,
    }
    warnings = []
    with_extras = generate_firmware(
        _MODEL, pin_map, "main", target="host", warn=warnings.append
    )

    assert len(warnings) == 2
    assert "'unused_clk' (pin -1)" in warnings[0]
    assert "'unused_led' (pin 25)" in warnings[1]

    # -- The unused entries have no effect on the generated code.
    without_extras = generate_firmware(
        _MODEL, {"clk": INTERNAL_PIN, "a": 0, "y": 1}, "main", target="host"
    )
    assert with_extras == without_extras


def test_unused_pcf_entry_does_not_claim_its_gpio():
    """An ignored entry must not count as holding a GPIO, so a real port
    can use the same pin without a duplicate-assignment error."""

    ports = parse_model_ports(_MODEL, "main")
    _, unused = resolve_ports(ports, {"clk": 2, "a": 0, "y": 1, "stale_y": 1})
    assert unused == ["stale_y"]


def test_missing_mapping_is_still_an_error():
    """A top-level port with no PCF entry remains a hard error."""
    ports = parse_model_ports(_MODEL, "main")
    with pytest.raises(CxxrtlError, match="no pin mapping for 'y'"):
        resolve_ports(ports, {"clk": INTERNAL_PIN, "a": 0})


def test_max_gpio():
    """The GPIO range check follows the chip's max-gpio."""
    ports = parse_model_ports(_MODEL, "main")
    pin_map = {"clk": INTERNAL_PIN, "a": 0, "y": 30}
    with pytest.raises(CxxrtlError, match="0 through 29"):
        resolve_ports(ports, pin_map)
    resolved, _ = resolve_ports(ports, pin_map, max_gpio=47)
    assert resolved[2].pins == [30]


def test_cmake_template_selects_board_and_platform():
    """The pico-sdk board and platform reach the generated CMakeLists."""
    text = runtime._CMAKE_LISTS_TEMPLATE.format(  # pylint: disable=W0212
        cpp_file_name="main.cc",
        cxxrtl_runtime_dir="/x",
        pico_board="pico2",
        pico_platform="rp2350-arm-s",
    )
    # -- Must be set before the SDK is included, or they have no effect.
    include_pos = text.index("pico_sdk_init.cmake")
    assert -1 < text.index("set(PICO_PLATFORM rp2350-arm-s)") < include_pos
    assert -1 < text.index("set(PICO_BOARD pico2)") < include_pos


def test_find_bootsel_volume_by_name(tmp_path, monkeypatch):
    """The Linux lookup matches the requested volume label only."""

    mounts = tmp_path / "mounts"
    mounts.write_text(
        f"/dev/sdb1 {tmp_path}/RP2350 vfat rw 0 0\n", encoding="utf-8"
    )
    real_open = open

    def fake_open(path, *args, **kwargs):
        if path == "/proc/mounts":
            path = mounts
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(upload.sys, "platform", "linux")
    monkeypatch.setattr("builtins.open", fake_open)
    # -- Don't let the fallback try to mount a real device.
    monkeypatch.setattr(
        upload, "_mount_bootsel_volume_linux", lambda _name: None
    )

    assert upload.find_bootsel_volume("RP2350") == Path(tmp_path / "RP2350")
    assert upload.find_bootsel_volume("RPI-RP2") is None
    assert upload.find_bootsel_volume() is None


def test_picotool_always_targets_one_board(monkeypatch):
    """picotool must never be left to pick a board itself: with a Pico and
    a Pico 2 both attached, that flashed the RP2350 image onto the
    RP2040. Device selection must also precede the filename, or picotool
    rejects it when combined with -f."""

    calls = []

    def fake_run_bounded(cmd, timeout=None):
        _ = timeout
        calls.append(cmd)

    monkeypatch.setattr(upload, "_run_bounded", fake_run_bounded)
    uf2 = Path("fw.uf2")

    # pylint: disable=protected-access
    upload._run_picotool("picotool", uf2, ["--ser", "ABC"], force=True)
    upload._run_picotool(
        "picotool",
        uf2,
        upload._picotool_bootsel_selector("2E8A", "000F"),
        force=False,
    )
    assert calls == [
        ["picotool", "load", "-f", "--ser", "ABC", "fw.uf2"],
        [
            "picotool",
            "load",
            "-x",
            "--vid",
            "0x2E8A",
            "--pid",
            "0x000F",
            "fw.uf2",
        ],
    ]
