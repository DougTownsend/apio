"""Flashes a pico .uf2 firmware over USB.

Two steps, matching the plan's "manual BOOTSEL once, hands-off after that"
design: if the board is already running apio pico firmware (a normal serial
device enumerates), send it a 'b' byte over its USB-serial port -- the
firmware's check_bootloader_trigger() (apio/pico/codegen.py) responds by
calling the pico-sdk's reset_usb_boot(), rebooting into the USB
mass-storage bootloader. Then flash the new .uf2 over USB once the board
re-enumerates in BOOTSEL mode, preferring `picotool load -f` but falling
back to a plain mass-storage file copy if picotool can't (see flash_uf2()).

A factory-fresh (or bricked) board with no firmware running won't respond
to the serial trigger -- in that case the student holds BOOTSEL while
plugging in USB, and this script's flashing step still works, it's only
the serial-trigger step that's skipped (silently, since no matching serial
device will be found).

Invoked as `python -m apio.pico.upload <uf2_path>`, matching apio's
programmer-cmd / ${BIN_FILE} convention (apio/managers/programmers.py) so
it plugs into `apio upload` as an ordinary programmer command.
"""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import serial

from apio.utils.serial_util import scan_serial_devices, SerialDeviceFilter

# -- Matches apio/pico/codegen.py's check_bootloader_trigger().
_REBOOT_TRIGGER_BYTE = b"b"

# -- How long to wait, after sending the reboot trigger, for the board to
# -- re-enumerate as a BOOTSEL mass-storage/picoboot device.
_BOOTSEL_WAIT_SECONDS = 5.0
_BOOTSEL_POLL_INTERVAL_SECONDS = 0.25

# -- Volume name the RP2040 bootloader presents itself as (confirmed via
# -- INFO_UF2.TXT's "Board-ID: RPI-RP2" on real hardware).
_BOOTSEL_VOLUME_NAME = "RPI-RP2"


class UploadError(Exception):
    """Raised when the pico upload flow fails."""


def find_running_firmware_port(
    vendor_id: str, product_id: str
) -> Optional[str]:
    """Returns the serial port of an already-running apio pico firmware
    board, if one is connected, else None (e.g. a factory-fresh board,
    or one already sitting in BOOTSEL mode, has no such port).

    scan_serial_devices() takes an ApioContext argument but doesn't use
    it (its parameter is named '_' in apio/utils/serial_util.py), so we
    pass None rather than constructing a full ApioContext here."""

    devices = scan_serial_devices(None)
    matches = (
        SerialDeviceFilter()
        .set_vendor_id(vendor_id)
        .set_product_id(product_id)
        .filter(devices)
    )
    if not matches:
        return None
    return matches[0].port


def trigger_bootloader(port: str) -> None:
    """Sends the reboot-to-bootloader byte over an open serial port."""
    with serial.Serial(port, baudrate=115200, timeout=1) as ser:
        ser.write(_REBOOT_TRIGGER_BYTE)
        ser.flush()


def find_bootsel_volume() -> Optional[Path]:
    """Returns the mount point of the RP2040 bootloader's mass-storage
    volume, if currently mounted, else None.

    picotool talks to the board's PICOBOOT USB interface directly, which
    on Linux needs a udev rule installed for non-root access (picotool
    ships one, but it's a separate one-time root-owned setup step -- see
    flash_uf2()). The plain FAT volume the same BOOTSEL-mode board also
    presents itself as needs no such setup: any desktop that auto-mounts
    USB drives (or a user session with polkit/udisks, as confirmed
    working here) can write to it with normal user permissions."""

    if sys.platform == "darwin":
        candidate = Path("/Volumes") / _BOOTSEL_VOLUME_NAME
        return candidate if candidate.is_dir() else None

    if sys.platform == "win32":
        return _find_bootsel_volume_windows()

    # -- Linux: scan /proc/mounts for a mountpoint named RPI-RP2 (matches
    # -- what desktop auto-mount / udisksctl both produce, e.g.
    # -- /media/<user>/RPI-RP2).
    mountpoint = _find_mounted_bootsel_volume_linux()
    if mountpoint:
        return mountpoint

    # -- Not found mounted. Most desktops auto-mount a newly attached USB
    # -- drive, but not all do (confirmed on the machine used to develop
    # -- this: udisks2 is present and working, but nothing auto-mounts on
    # -- attach) -- so also try mounting it ourselves via udisksctl, which
    # -- needs no root/sudo (it's a normal user-session polkit action).
    return _mount_bootsel_volume_linux()


def _find_mounted_bootsel_volume_linux() -> Optional[Path]:
    try:
        with open("/proc/mounts", encoding="utf-8") as f:
            for line in f:
                fields = line.split()
                if len(fields) < 2:
                    continue
                mountpoint = Path(fields[1])
                if mountpoint.name == _BOOTSEL_VOLUME_NAME:
                    return mountpoint
    except OSError:
        pass
    return None


def _mount_bootsel_volume_linux() -> Optional[Path]:
    udisksctl = shutil.which("udisksctl")
    lsblk = shutil.which("lsblk")
    if not udisksctl or not lsblk:
        return None

    # -- Find the (currently unmounted) partition with label RPI-RP2.
    result = subprocess.run(
        [lsblk, "-o", "NAME,LABEL,PATH", "-J"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None

    import json  # pylint: disable=import-outside-toplevel

    device_path = None
    try:
        tree = json.loads(result.stdout)
        for device in tree.get("blockdevices", []):
            for child in device.get("children", []) or []:
                if child.get("label") == _BOOTSEL_VOLUME_NAME:
                    device_path = child.get("path")
    except (ValueError, KeyError):
        return None

    if not device_path:
        return None

    mount_result = subprocess.run(
        [udisksctl, "mount", "-b", device_path],
        capture_output=True,
        text=True,
        check=False,
    )
    if mount_result.returncode != 0:
        return None

    # -- udisksctl prints e.g. "Mounted /dev/sda1 at /media/doug/RPI-RP2."
    for word in mount_result.stdout.split():
        if word.startswith("/") and word.rstrip(".").endswith(
            _BOOTSEL_VOLUME_NAME
        ):
            return Path(word.rstrip("."))
    # -- Mounted successfully but couldn't parse the path from stdout;
    # -- fall back to re-scanning mounts.
    return _find_mounted_bootsel_volume_linux()


def _find_bootsel_volume_windows() -> Optional[Path]:
    """Scans drive letters for one labeled RPI-RP2, via ctypes so no
    extra dependency (e.g. pywin32) is required."""
    import ctypes  # pylint: disable=import-outside-toplevel
    import string  # pylint: disable=import-outside-toplevel

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    for letter in string.ascii_uppercase:
        root = f"{letter}:\\"
        if kernel32.GetDriveTypeW(root) != 2:  # DRIVE_REMOVABLE
            continue
        name_buf = ctypes.create_unicode_buffer(261)
        if kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(root),
            name_buf,
            ctypes.sizeof(name_buf),
            None,
            None,
            None,
            None,
            0,
        ):
            if name_buf.value == _BOOTSEL_VOLUME_NAME:
                return Path(root)
    return None


def flash_via_mass_storage(uf2_path: Path) -> None:
    """Fallback flash path: copy the .uf2 straight onto the mounted
    RP2040 bootloader volume, polling for it to appear."""
    deadline = time.monotonic() + _BOOTSEL_WAIT_SECONDS
    volume = None
    while time.monotonic() < deadline:
        volume = find_bootsel_volume()
        if volume:
            break
        time.sleep(_BOOTSEL_POLL_INTERVAL_SECONDS)

    if not volume:
        raise UploadError(
            f"could not find a mounted {_BOOTSEL_VOLUME_NAME} volume -- "
            "is the board in BOOTSEL mode (hold BOOTSEL while plugging "
            "in USB)?"
        )

    shutil.copy(uf2_path, volume / uf2_path.name)
    if hasattr(os, "sync"):
        os.sync()


def flash_uf2(uf2_path: Path) -> None:
    """Flashes uf2_path to a board in BOOTSEL mode. Prefers `picotool
    load -f` (faster, more reliable device detection); if picotool isn't
    installed, or fails (e.g. the udev rule for non-root PICOBOOT access
    isn't set up -- confirmed to happen on a real, otherwise-working
    Linux machine during testing), falls back to a plain mass-storage
    file copy, which needs no extra permissions setup at all."""
    picotool = shutil.which("picotool")
    last_error = None

    if picotool:
        deadline = time.monotonic() + _BOOTSEL_WAIT_SECONDS
        while time.monotonic() < deadline:
            result = subprocess.run(
                [picotool, "load", "-f", str(uf2_path)],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode == 0:
                return
            last_error = result.stderr or result.stdout
            time.sleep(_BOOTSEL_POLL_INTERVAL_SECONDS)
        print(
            f"picotool failed ({last_error.strip() if last_error else ''}"
            "), falling back to mass-storage copy"
        )

    flash_via_mass_storage(uf2_path)


def upload(
    uf2_path: Path,
    vendor_id: str = "2E8A",
    product_id: str = "000A",
) -> None:
    """Full upload flow: trigger a remote reboot into the bootloader if
    the board is already running apio pico firmware, then flash."""

    port = find_running_firmware_port(vendor_id, product_id)
    if port:
        print(f"Rebooting pico into bootloader mode via {port}")
        trigger_bootloader(port)
        time.sleep(0.5)
    else:
        print(
            "No running apio pico firmware detected -- assuming the "
            "board is already in BOOTSEL mode (or hold BOOTSEL while "
            "plugging in USB for a first flash)."
        )

    print(f"Flashing {uf2_path}")
    flash_uf2(uf2_path)
    print("Upload complete.")


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: python -m apio.pico.upload <uf2_path>", file=sys.stderr)
        return 2

    try:
        upload(Path(argv[0]))
    except UploadError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
