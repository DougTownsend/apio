"""Flashes a pico .uf2 firmware over USB, from either board state.

A Pico presents two different USB identities, and `apio upload` has to
work with whichever one is in front of it:

  - Running the apio firmware: a CDC serial device at 2E8A:000A. Here we
    send it a 'b' byte over that port -- the firmware's
    check_bootloader_trigger() (apio/pico/codegen.py) responds by calling
    the pico-sdk's reset_usb_boot(), rebooting into the bootloader -- and
    then wait for the port to disappear, confirming the reset took.

  - Already in BOOTSEL: a mass-storage/PICOBOOT device at 2E8A:0003, with
    no serial port at all. Nothing to trigger; we go straight to flashing.
    This covers both a factory-fresh board and one the user put into
    BOOTSEL by hand (holding the button while plugging in USB).

Both paths converge on flash_uf2(), which copies the .uf2 onto the
bootloader's RPI-RP2 volume, and falls back to `picotool load -f` only if
that volume never appears (as can happen over SSH on a headless machine).
The copy comes first because it needs no privileged setup anywhere, while
picotool needs a udev rule on Linux or a Zadig-installed WinUSB driver on
Windows.

Because the board's identity depends on its mode, the "pico" board
definition deliberately carries no "usb" section -- apio's board-level
presence check is a single filter and couldn't express "either of these"
anyway. Device detection lives here instead. See boards.jsonc in the
example projects.

Invoked as `python -m apio.pico.upload <uf2_path>`, matching apio's
programmer-cmd / ${BIN_FILE} convention (apio/managers/programmers.py) so
it plugs into `apio upload` as an ordinary programmer command.
"""

import argparse
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

# -- USB ids of a Pico running the apio firmware, i.e. pico-sdk's
# -- stdio-over-USB CDC device (pico_enable_stdio_usb in
# -- apio/pico/runtime.py's CMakeLists template).
_FIRMWARE_VID = "2E8A"
_FIRMWARE_PID = "000A"

# -- How long to hold the serial port open after writing the trigger byte.
# -- Closing the port drops DTR, and pico-sdk's stdio_usb only hands
# -- buffered input to the firmware while the CDC port is connected -- so
# -- closing immediately after the write races the firmware's ~1ms
# -- tud_task poll and the byte is silently dropped. Observed in practice:
# -- one upload rebooted the board, the very next one didn't touch it.
_TRIGGER_DWELL_SECONDS = 0.3

# -- How long to wait, after sending the reboot trigger, for the board to
# -- drop off the bus (its serial port disappears).
_REBOOT_WAIT_SECONDS = 3.0

# -- How many times to send the trigger before giving up. The write itself
# -- can't be acknowledged (the board reboots instead of replying), so the
# -- port disappearing is the only confirmation available.
_TRIGGER_ATTEMPTS = 3

# -- How long to keep trying to flash a board in BOOTSEL mode. Covers the
# -- USB re-enumeration after a triggered reboot, plus the time a desktop
# -- takes to auto-mount the bootloader volume on the mass-storage path.
_BOOTSEL_WAIT_SECONDS = 10.0

_POLL_INTERVAL_SECONDS = 0.5

# -- Volume name the RP2040 bootloader presents itself as (confirmed via
# -- INFO_UF2.TXT's "Board-ID: RPI-RP2" on real hardware).
_BOOTSEL_VOLUME_NAME = "RPI-RP2"


class UploadError(Exception):
    """Raised when the pico upload flow fails."""


def _say(message: str) -> None:
    """Prints a progress message, flushing as it goes.

    `apio upload` runs this module as a scons subprocess with its stdout
    piped, which makes stdout block-buffered while stderr stays unbuffered
    -- so without the flush, progress lines show up *after* any error
    message rather than before it."""
    print(message, flush=True)


def _normalize_usb_id(usb_id: str) -> str:
    """apio's device filters require 4-char uppercase hex (see
    check_usb_id_format() in apio/utils/usb_util.py), but board and
    programmer definitions conventionally spell USB ids in lowercase.
    Accept either rather than raising ValueError on '2e8a'."""
    return usb_id.strip().upper()


def find_running_firmware_port(
    vendor_id: str = _FIRMWARE_VID, product_id: str = _FIRMWARE_PID
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
        .set_vendor_id(_normalize_usb_id(vendor_id))
        .set_product_id(_normalize_usb_id(product_id))
        .filter(devices)
    )
    if not matches:
        return None
    return matches[0].port


def trigger_bootloader(port: str) -> None:
    """Sends the reboot-to-bootloader byte over an open serial port.

    The firmware calls reset_usb_boot() as soon as it reads the byte, so
    the device can vanish while we're still writing to or closing the
    port. A SerialException at that point means the reset worked, so it's
    tolerated -- but a failure to *open* the port is a real error worth
    reporting (e.g. no permission on the tty, or another program holding
    it open)."""

    try:
        ser = serial.Serial(port, baudrate=115200, timeout=1)
    except serial.SerialException as e:
        raise UploadError(
            f"could not open {port} to reboot the board into BOOTSEL "
            f"mode: {e}"
        ) from e

    try:
        ser.dtr = True
        ser.write(_REBOOT_TRIGGER_BYTE)
        ser.flush()
        # -- Keep the port open (DTR asserted) long enough for the
        # -- firmware to actually poll and act on the byte. See
        # -- _TRIGGER_DWELL_SECONDS.
        time.sleep(_TRIGGER_DWELL_SECONDS)
    except serial.SerialException:
        # -- Board reset mid-write. That was the point.
        pass
    finally:
        try:
            ser.close()
        except serial.SerialException:
            pass


def reboot_into_bootsel(port: str) -> bool:
    """Gets a board running apio firmware into BOOTSEL mode, retrying the
    trigger. Returns True once its serial port is gone.

    Retrying is worth it because the trigger is fire-and-forget: the board
    reboots rather than replying, so a dropped byte is indistinguishable
    from a slow reboot except by waiting."""

    for attempt in range(_TRIGGER_ATTEMPTS):
        # -- Already gone (either a previous attempt landed, or the board
        # -- rebooted on its own between the scan and now).
        if port not in {d.port for d in scan_serial_devices(None)}:
            return True

        trigger_bootloader(port)
        if wait_for_port_to_disappear(port):
            return True

        if attempt + 1 < _TRIGGER_ATTEMPTS:
            _say(f"Board is still on {port}; re-sending the trigger.")

    return False


def wait_for_port_to_disappear(
    port: str, timeout: float = _REBOOT_WAIT_SECONDS
) -> bool:
    """Polls until `port` is no longer enumerated, i.e. the board has left
    application mode. Returns True if it went away within the timeout."""

    deadline = time.monotonic() + timeout
    while True:
        if port not in {d.port for d in scan_serial_devices(None)}:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_POLL_INTERVAL_SECONDS)


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


def flash_via_mass_storage(uf2_path: Path, volume: Path) -> None:
    """Fallback flash path: copy the .uf2 straight onto the mounted
    RP2040 bootloader volume. The bootloader reboots into the new
    firmware as soon as the write lands."""
    shutil.copy(uf2_path, volume / uf2_path.name)
    if hasattr(os, "sync"):
        os.sync()


def _run_picotool(picotool: str, uf2_path: Path) -> Optional[str]:
    """Runs `picotool load -f`. Returns None on success, else its
    diagnostic output."""
    result = subprocess.run(
        [picotool, "load", "-f", str(uf2_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return None
    return (result.stderr or result.stdout or "").strip()


def _no_board_message(
    picotool: Optional[str],
    picotool_error: Optional[str],
    reboot_failed: bool = False,
) -> str:
    """Builds the diagnostic for 'nothing flashable showed up', covering
    each reason a board might not be there.

    This is the single place upload diagnostics surface. Everything the
    run learned along the way is collected here rather than printed as it
    happens, so a recovered failure stays silent and a real one explains
    itself completely."""
    lines = [
        "no Raspberry Pi Pico in BOOTSEL mode found after "
        f"{int(_BOOTSEL_WAIT_SECONDS)}s.",
        "- For the first flash of a new board, hold the BOOTSEL button "
        "while plugging in USB, then run 'apio upload' again.",
    ]
    if reboot_failed:
        lines.append(
            "- The board was running apio firmware but did not respond to "
            "the reboot trigger, so it never entered BOOTSEL mode. Unplug "
            "and replug it, holding BOOTSEL."
        )
    else:
        lines.append(
            "- If the board is running apio firmware it should have "
            "rebooted itself; try unplugging and replugging it."
        )
    lines.append(
        f"- No {_BOOTSEL_VOLUME_NAME} volume appeared. On a headless "
        "machine it may not be mounted automatically; mounting it by hand "
        "and re-running is enough."
    )
    if not picotool:
        lines.append(
            "- 'picotool' was not found on PATH, so the direct-USB "
            "fallback was unavailable. Run 'apio packages install'."
        )
    elif picotool_error:
        lines.append(f"- picotool also failed: {picotool_error}")
    return "\n".join(lines)


def flash_uf2(uf2_path: Path, reboot_failed: bool = False) -> None:
    """Flashes uf2_path to a board in BOOTSEL mode, polling until one
    shows up (it may still be re-enumerating after a triggered reboot).

    `reboot_failed` says the board ignored the reboot trigger. It only
    affects the wording if the flash then fails too; on success it stays
    unmentioned, since a board that got into BOOTSEL some other way is
    not a problem worth reporting.

    Each round copies the .uf2 onto the bootloader's RPI-RP2 volume if it
    can, and only falls back to `picotool load -f` if it can't. That order
    is deliberate: the copy is the path that needs no privileged setup on
    any platform, and in testing it is the one that actually performed
    every successful flash. picotool talks to the board's PICOBOOT
    interface directly, which needs a driver the user is unlikely to
    have -- a udev rule on Linux, or a WinUSB driver installed via Zadig
    on Windows -- and it failed on every real machine tried.

    picotool is kept rather than dropped because the two paths fail in
    different circumstances, not the same ones. The copy needs the volume
    to be mounted, which relies on desktop auto-mount or the udisksctl
    call in find_bootsel_volume(), and that can fail over SSH on a
    headless machine with no local session for polkit to grant against.
    There, picotool plus the udev rule is what works.

    Nothing is reported while alternatives remain untried: a failure that
    a later path recovers from is not a failure of the upload, and
    printing its (long, alarming) diagnostic mid-flow made successful
    uploads look broken. Diagnostics are held back and emitted only once
    everything is exhausted -- see _no_board_message()."""

    picotool = shutil.which("picotool")
    picotool_error = None
    deadline = time.monotonic() + _BOOTSEL_WAIT_SECONDS

    while True:
        volume = find_bootsel_volume()
        if volume:
            _say(f"Copying to {volume}")
            flash_via_mass_storage(uf2_path, volume)
            return

        if picotool:
            picotool_error = _run_picotool(picotool, uf2_path)
            if picotool_error is None:
                _say("Flashed with picotool.")
                return

        if time.monotonic() >= deadline:
            raise UploadError(
                _no_board_message(picotool, picotool_error, reboot_failed)
            )
        time.sleep(_POLL_INTERVAL_SECONDS)


def upload(
    uf2_path: Path,
    vendor_id: str = _FIRMWARE_VID,
    product_id: str = _FIRMWARE_PID,
) -> None:
    """Full upload flow, tolerant of either board state: reboot the board
    into its bootloader if it's running apio firmware, then flash. A
    board already in BOOTSEL mode skips straight to the flash."""

    if not uf2_path.is_file():
        raise UploadError(f"firmware file not found: {uf2_path}")

    reboot_failed = False
    port = find_running_firmware_port(vendor_id, product_id)
    if port:
        _say(f"Board is running apio firmware on {port}.")
        _say("Rebooting it into BOOTSEL mode.")
        # -- Not reported here even when it fails. The flash below may
        # -- still succeed (the board can be in BOOTSEL for other
        # -- reasons), and saying so mid-flow would make a working upload
        # -- look broken. It is carried into the failure message instead.
        reboot_failed = not reboot_into_bootsel(port)
    else:
        _say(
            "No running apio firmware detected -- expecting a board "
            "already in BOOTSEL mode."
        )

    _say(f"Flashing {uf2_path}")
    flash_uf2(uf2_path, reboot_failed=reboot_failed)
    _say("Upload complete.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m apio.pico.upload",
        description=(
            "Flash a .uf2 firmware to a Raspberry Pi Pico, whether it is "
            "running apio firmware or already sitting in BOOTSEL mode."
        ),
    )
    parser.add_argument(
        "--vid",
        default=_FIRMWARE_VID,
        help=(
            "USB vendor id of a board running the apio firmware "
            f"(default: {_FIRMWARE_VID})"
        ),
    )
    parser.add_argument(
        "--pid",
        default=_FIRMWARE_PID,
        help=(
            "USB product id of a board running the apio firmware "
            f"(default: {_FIRMWARE_PID})"
        ),
    )
    parser.add_argument("uf2_path", help="path of the .uf2 file to flash")

    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    try:
        upload(
            Path(args.uf2_path), vendor_id=args.vid, product_id=args.pid
        )
    except UploadError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
