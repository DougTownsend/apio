"""Compiles the codegen'd C firmware into a .uf2 using pico-sdk + CMake +
arm-none-eabi-gcc.

This is the one part of the pico target that genuinely needs the real
toolchain (arm-none-eabi-gcc, cmake, pico-sdk) installed to exercise --
it has not yet been run end-to-end against real hardware. Wiring these
three tools into apio's package system (`apio packages install`) the same
way `oss-cad-suite` is distributed today is tracked separately; for now
this module expects them to be discoverable via PATH / PICO_SDK_PATH.
"""

import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from apio.utils import util

_CMAKE_LISTS_TEMPLATE = """\
cmake_minimum_required(VERSION 3.13)
include($ENV{{PICO_SDK_PATH}}/pico_sdk_init.cmake)
project(apio_pico_firmware C CXX ASM)
set(CMAKE_C_STANDARD 11)
pico_sdk_init()
add_executable(firmware {c_file_name})
target_link_libraries(firmware pico_stdlib)
pico_enable_stdio_usb(firmware 1)
pico_enable_stdio_uart(firmware 0)
pico_add_extra_outputs(firmware)
"""


class PicoBuildError(Exception):
    """Raised when the native compile stage fails."""


def _require_tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise PicoBuildError(
            f"required tool {name!r} not found on PATH -- install it via "
            "'apio packages install' (or your system package manager) "
            "before building for the pico target"
        )
    return path


def _apio_packages_dir() -> Path:
    """Root of the installed apio packages.

    Defers to apio's own resolver rather than reimplementing it, so that
    APIO_HOME and APIO_PACKAGES are honored here exactly as they are
    everywhere else. Guessing at the location instead would appear to
    work on a default install -- where it resolves to the same
    ~/.apio/packages -- and silently read the wrong directory anywhere
    those variables are set, e.g. a CI or autograder container."""

    return util.resolve_packages_dir(util.resolve_home_dir())


def _find_package_dir(name: str, marker: str) -> Optional[Path]:
    """Locates an installed package's payload directory.

    Packages are laid out as <packages>/<name>-<platform>/<name>-<version>,
    so both levels are globbed rather than hardcoding a version. `marker`
    is a path that must exist inside the result, which both validates the
    directory and disambiguates a partially-extracted package."""

    packages_dir = _apio_packages_dir()
    if not packages_dir.is_dir():
        return None

    for package_dir in sorted(packages_dir.glob(f"{name}-*")):
        if not package_dir.is_dir():
            continue
        # -- The versioned payload directory inside the package.
        for payload_dir in sorted(package_dir.glob(f"{name}-*")):
            if (payload_dir / marker).exists():
                return payload_dir
        # -- Some archives extract without a wrapping versioned directory.
        if (package_dir / marker).exists():
            return package_dir

    return None


def _get_pico_sdk_path() -> str:
    """Returns the pico-sdk root, from $PICO_SDK_PATH if set, else from
    the installed apio package."""

    env_path = os.environ.get("PICO_SDK_PATH")
    if env_path and Path(env_path).is_dir():
        return env_path

    sdk_dir = _find_package_dir("pico-sdk", "pico_sdk_init.cmake")
    if sdk_dir:
        return str(sdk_dir)

    raise PicoBuildError(
        "PICO_SDK_PATH is not set and pico-sdk was not found in the apio "
        "packages -- run 'apio packages install' before building for the "
        "pico target"
    )


def _get_tinyusb_path() -> str:
    """Returns the TinyUSB root, from $PICO_TINYUSB_PATH if set, else from
    the installed apio package.

    This is required, not optional. The pico-sdk release tarball ships
    without its git submodules, so its bundled lib/tinyusb is empty; when
    the SDK can't find TinyUSB it emits a *warning* and quietly disables
    USB support, turning pico_enable_stdio_usb() into a no-op. The build
    still succeeds, but the firmware never enumerates over USB -- so it
    can't be rebooted into BOOTSEL and every 'apio upload' would need the
    button pressed by hand. Failing loudly here beats shipping that."""

    marker = "hw/bsp/rp2040"

    env_path = os.environ.get("PICO_TINYUSB_PATH")
    if env_path and (Path(env_path) / marker).exists():
        return env_path

    tinyusb_dir = _find_package_dir("tinyusb", marker)
    if tinyusb_dir:
        return str(tinyusb_dir)

    raise PicoBuildError(
        "TinyUSB was not found in the apio packages -- run 'apio packages "
        "install'. Without it the pico-sdk silently builds firmware with "
        "no USB support, which cannot be flashed without pressing BOOTSEL."
    )


def build_uf2(generated_c: Path, uf2_target: Path) -> int:
    """Builds `generated_c` into a .uf2 at `uf2_target`. Returns 0 on
    success, non-zero (and prints diagnostics) on failure, matching the
    SCons Action function-action convention."""

    try:
        _require_tool("cmake")
        _require_tool("arm-none-eabi-gcc")
        os.environ["PICO_SDK_PATH"] = _get_pico_sdk_path()
        os.environ["PICO_TINYUSB_PATH"] = _get_tinyusb_path()

        build_dir = generated_c.parent / "_pico_cmake_build"
        build_dir.mkdir(exist_ok=True)

        cmake_lists = build_dir / "CMakeLists.txt"
        cmake_lists.write_text(
            _CMAKE_LISTS_TEMPLATE.format(c_file_name=generated_c.name),
            encoding="utf-8",
        )
        shutil.copy(generated_c, build_dir / generated_c.name)

        subprocess.run(
            ["cmake", "-S", str(build_dir), "-B", str(build_dir)],
            check=True,
        )
        subprocess.run(
            ["cmake", "--build", str(build_dir), "--target", "firmware"],
            check=True,
        )

        built_uf2 = build_dir / "firmware.uf2"
        if not built_uf2.exists():
            raise PicoBuildError(
                f"cmake build finished but {built_uf2} was not produced"
            )
        shutil.copy(built_uf2, uf2_target)
        return 0

    except (PicoBuildError, subprocess.CalledProcessError) as e:
        print(f"Pico firmware build failed: {e}")
        return 1
