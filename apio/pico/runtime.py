"""Compiles CXXRTL-based C++ firmware into a .uf2 using pico-sdk + CMake.

The compiler, SDK, Ninja, picotool, and TinyUSB are resolved from Apio's
installed packages, with environment/PATH fallbacks for development setups.
"""

import hashlib
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
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
pico_sdk_init()
add_executable(firmware {cpp_file_name})
target_include_directories(firmware PRIVATE "{cxxrtl_runtime_dir}")
target_compile_definitions(firmware PRIVATE CXXRTL_NDEBUG)
target_compile_options(
  firmware PRIVATE $<$<COMPILE_LANGUAGE:CXX>:-fno-exceptions;-fno-rtti>)
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
        # -- The payload directory inside the package. Most packages use a
        # -- versioned name; a few (notably picotool) use just the package
        # -- name, so inspect every immediate child.
        for payload_dir in sorted(package_dir.iterdir()):
            if not payload_dir.is_dir():
                continue
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


def _get_ninja_exe() -> str:
    """Returns the path of the ninja executable from the apio packages.

    The pico firmware build pins its cmake generator to Ninja rather than
    letting cmake choose. Left to itself cmake picks from what happens to
    be on the host: 'NMake Makefiles' on Windows, which requires Visual
    Studio and fails outright on a machine that only has Python and apio,
    and 'Unix Makefiles' elsewhere, which needs make to be installed. The
    packaged ninja is present on every platform by construction, so the
    firmware builds identically everywhere."""

    exe_name = "ninja.exe" if os.name == "nt" else "ninja"

    ninja_dir = _find_package_dir("ninja", exe_name)
    if ninja_dir:
        return str(ninja_dir / exe_name)

    # -- Fall back to a copy on PATH before giving up, so a developer with
    # -- their own ninja isn't blocked by a missing package.
    on_path = shutil.which("ninja")
    if on_path:
        return on_path

    raise PicoBuildError(
        "ninja was not found in the apio packages -- run 'apio packages "
        "install'. It is required to build the pico firmware."
    )


def _get_picotool_dir() -> Optional[Path]:
    """Returns a packaged picotool CMake config directory, if available."""

    env_path = os.environ.get("picotool_DIR")
    if env_path and (Path(env_path) / "picotoolConfig.cmake").exists():
        return Path(env_path)
    return _find_package_dir("picotool", "picotoolConfig.cmake")


def build_uf2(
    generated_cpp: Path,
    uf2_target: Path,
    cxxrtl_runtime_dir: Path,
) -> int:
    """Builds `generated_cpp` into a .uf2 at `uf2_target`. Returns 0 on
    success, non-zero (and prints diagnostics) on failure, matching the
    SCons Action function-action convention."""

    try:
        _require_tool("cmake")
        _require_tool("arm-none-eabi-gcc")
        _require_tool("arm-none-eabi-g++")
        if not (cxxrtl_runtime_dir / "cxxrtl" / "cxxrtl.h").exists():
            raise PicoBuildError(
                f"CXXRTL runtime headers not found at {cxxrtl_runtime_dir}"
            )
        sdk_path = _get_pico_sdk_path()
        tinyusb_path = _get_tinyusb_path()
        os.environ["PICO_SDK_PATH"] = sdk_path
        os.environ["PICO_TINYUSB_PATH"] = tinyusb_path

        build_dir = generated_cpp.parent / "_pico_cmake_build"
        cmake_text = _CMAKE_LISTS_TEMPLATE.format(
            cpp_file_name=generated_cpp.name,
            cxxrtl_runtime_dir=cxxrtl_runtime_dir.as_posix(),
        )

        # -- Pin the generator and point cmake straight at the packaged
        # -- ninja, rather than relying on it being found on PATH.
        ninja_exe = _get_ninja_exe()
        picotool_dir = _get_picotool_dir()

        # -- CMake caches the SDK toolchain and platform paths deeply. A
        # -- build tree configured with a different SDK cannot be repaired
        # -- by merely changing PICO_SDK_PATH; it combines both trees and
        # -- fails with misleading add_subdirectory errors. Keep a compact
        # -- identity for everything that affects configuration and discard
        # -- only this generated build directory when that identity changes.
        configure_identity = "\n".join(
            [
                cmake_text,
                sdk_path,
                tinyusb_path,
                str(cxxrtl_runtime_dir),
                ninja_exe,
                str(picotool_dir or ""),
            ]
        )
        configure_digest = hashlib.sha256(
            configure_identity.encode("utf-8")
        ).hexdigest()
        stamp_file = build_dir / ".apio-config"
        old_digest = (
            stamp_file.read_text(encoding="utf-8").strip()
            if stamp_file.exists()
            else None
        )
        if build_dir.exists() and old_digest != configure_digest:
            shutil.rmtree(build_dir)
        build_dir.mkdir(exist_ok=True)

        cmake_lists = build_dir / "CMakeLists.txt"
        cmake_lists.write_text(cmake_text, encoding="utf-8")
        shutil.copy(generated_cpp, build_dir / generated_cpp.name)

        # -- A cache left by a different generator makes configure fail
        # -- outright ("does not match the generator used previously"),
        # -- which would strand any build tree created before the
        # -- generator was pinned. Discard it and reconfigure.
        cache_file = build_dir / "CMakeCache.txt"
        if cache_file.exists():
            cached = cache_file.read_text(encoding="utf-8", errors="ignore")
            if "CMAKE_GENERATOR:INTERNAL=Ninja" not in cached:
                cache_file.unlink()
                shutil.rmtree(build_dir / "CMakeFiles", ignore_errors=True)
        configure_command = [
            "cmake",
            "-G",
            "Ninja",
            f"-DCMAKE_MAKE_PROGRAM={ninja_exe}",
        ]
        if picotool_dir:
            configure_command.append(f"-Dpicotool_DIR={picotool_dir}")
        configure_command.extend(
            ["-S", str(build_dir), "-B", str(build_dir)]
        )
        subprocess.run(configure_command, check=True)
        stamp_file.write_text(configure_digest + "\n", encoding="utf-8")
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
