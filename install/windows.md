# Installing apio on Windows

Everything here is done from **PowerShell** with `winget`, Windows' built-in
package manager. No websites to visit and nothing to download by hand.

`winget` ships with Windows 11 and recent Windows 10. Check it with:

```powershell
winget --version
```

If that fails, install "App Installer" from the Microsoft Store, which
provides it.

---

## 1. Install Git and Python

```powershell
winget install --id Git.Git --source winget --accept-package-agreements --accept-source-agreements
winget install --id Python.Python.3.13 --source winget --accept-package-agreements --accept-source-agreements
```

apio needs Python 3.10 or newer. Python installs per-user under
`%LOCALAPPDATA%\Programs\Python\Python313`.

> **Close this PowerShell window and open a new one before continuing.**
> Installers write `PATH` into the registry, and an already-running shell does
> not pick that up. You will need a fresh window after each install step below.

Check that Python is working:

```powershell
python --version
```

This should print `Python 3.13.x`. If it instead opens the Microsoft Store, see
[`python` opens the Store](#python-opens-the-microsoft-store) below.

## 2. Install pipx

pipx is not available through winget, so it comes from pip:

```powershell
python -m pip install --user pipx
python -m pipx ensurepath
```

Open a new PowerShell window, then check:

```powershell
pipx --version
```

## 3. Clone and install apio

```powershell
cd $HOME
git clone https://github.com/DougTownsend/apio.git
cd apio
pipx install --force .
```

Open a new PowerShell window, then check:

```powershell
apio --version
```

To update later, from inside that same clone:

```powershell
cd $HOME\apio
git pull origin main
pipx install --force .
```

## 4. Install the toolchain

Unlike upstream apio, this fork never downloads packages on its own. Fetch them
once, explicitly:

```powershell
apio packages install
```

This pulls yosys, the ARM compiler, CMake, Ninja, the Pico SDK and TinyUSB.
It is a large download and takes several minutes. Re-run it after any
`git pull` that changes the package definitions.

## 5. Check it works

```powershell
cd $HOME\apio\experimental\pico-hello
apio build
```

A successful build ends with `[SUCCESS]`.

---

## Connecting a Pico

No drivers are needed. Windows recognises both states of the board on its own:
a serial port when it is running your firmware, and a removable drive labelled
`RPI-RP2` when it is in its bootloader.

**Hold the BOOTSEL button while plugging in the USB cable**, then run:

```powershell
apio upload
```

On Linux, only the *first* flash of a board needs that button press — after
which apio reboots the board into its bootloader by itself. That hands-off
behaviour has not yet been confirmed on Windows, so for now expect to hold
BOOTSEL each time. If you find it works without, that is the intended
behaviour and worth reporting.

> If uploading does nothing, check the cable. Many USB cables sold with
> phones and battery packs carry power only, with no data wires, and a Pico on
> such a cable powers up with its LED on while remaining completely invisible
> to the computer.

---

## Troubleshooting

### `python` opens the Microsoft Store

Windows ships placeholder `python.exe` and `python3.exe` stubs that do nothing
but advertise the Store, and they can take precedence over a real install. Turn
them off at **Settings → Apps → Advanced app settings → App execution
aliases**, switching off both `python.exe` and `python3.exe`. Then open a new
PowerShell window.

### `python3` is not recognised

This is expected on Windows and is not a problem. The official Python installer
creates `python.exe` and `py.exe` but never `python3.exe`, so use `python`.
apio does not rely on the `python3` name.

### A command is "not recognized" right after installing it

Open a new PowerShell window. `PATH` changes do not reach shells that were
already open.

### `winget search` hangs waiting for input

A bare `winget search <name>` also queries the Microsoft Store source, which
stops to ask you to accept its terms. Add `--source winget` to search only
winget's own catalogue.

### apio upload mentions picotool and Zadig

You can ignore this. `picotool` is an optional accelerated path that needs a
driver Windows does not ship. apio falls back to copying the firmware onto the
board's `RPI-RP2` drive, which needs no driver, and reports `Upload complete`
when it succeeds. There is no need to install anything with Zadig.
