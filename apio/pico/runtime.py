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


def build_uf2(generated_c: Path, uf2_target: Path) -> int:
    """Builds `generated_c` into a .uf2 at `uf2_target`. Returns 0 on
    success, non-zero (and prints diagnostics) on failure, matching the
    SCons Action function-action convention."""

    try:
        _require_tool("cmake")
        _require_tool("arm-none-eabi-gcc")
        if not os.environ.get("PICO_SDK_PATH"):
            raise PicoBuildError(
                "PICO_SDK_PATH is not set -- point it at an installed "
                "pico-sdk checkout"
            )

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
