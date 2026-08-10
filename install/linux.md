# Installing apio on Linux

Instructions below use `apt` (Debian, Ubuntu, Linux Mint, Pop!_OS). For other
distributions, see [Other distributions](#other-distributions).

> **If you have installed apio before**, uninstall it first. If you have an old version installed elsewhere, it may take precedence over the version we're installing here, causing confusing errors. Check with `which apio` to see if an older version exists, and uninstall it before proceeding.

---

## 1. Install Git, Python and pipx

```bash
sudo apt update
sudo apt install -y git python3 python3-venv pipx
pipx ensurepath
```

apio needs Python 3.10 or newer, which every currently supported Ubuntu
release provides. Check with:

```bash
python3 --version
```

## 2. Close and reopen the terminal

**Close this terminal and open a new one.** The `pipx ensurepath` command adds `~/.local/bin` to your `PATH`, and this change only takes effect in a new shell.

## 3. Clone and install apio

```bash
cd ~
git clone https://github.com/DougTownsend/apio.git
cd apio
pipx install --force .
```

Check it:

```bash
apio --version
```

To update later, from inside that same clone:

```bash
cd ~/apio
git pull origin main
pipx install --force .
```

## 4. Install the toolchain

Unlike upstream apio, this fork never downloads packages on its own. Fetch them
once, explicitly:

```bash
apio packages install
```

This pulls yosys, the ARM compiler, CMake, Ninja, the Pico SDK and TinyUSB.
It is a large download and takes several minutes. Re-run it after any
`git pull` that changes the package definitions.

## 5. Check it works

```bash
cd ~/apio/experimental/pico-hello
apio build
```

A successful build ends with `[SUCCESS]`.

---

## 6. Allow access to the Pico

By default Linux only lets root talk to USB serial devices, so this step is
needed before `apio upload` can reboot a board that is already running your
firmware. Install the rules file included in the repo:

```bash
sudo cp ~/apio/scripts/99-apio-pico.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger
```

Then unplug and replug the board. No logout is needed — the rules grant access
to whoever is logged in at the machine.

<details>
<summary>Alternative: add yourself to the <code>dialout</code> group</summary>

If you would rather not install a rules file:

```bash
sudo usermod -aG dialout $USER
```

You must then **log out and back in**, since group membership is only applied
at login. This grants serial-port access but not the direct USB access that
the optional `picotool` path uses; that path is not required, so uploads still
work.

</details>

---

## 7. Connecting a Pico

**The first time you flash a particular board**, hold the BOOTSEL button while
plugging in the USB cable, then run:

```bash
apio upload
```

After that first flash, `apio upload` handles the reboot itself and you can
leave the button alone.

> If uploading does nothing, check the cable. Many USB cables sold with phones
> and battery packs carry power only, with no data wires, and a Pico on such a
> cable powers up with its LED on while remaining completely invisible to the
> computer.

---

## 8. Other distributions

Install the same three things — `git`, Python 3.10+, and `pipx` — with your own
package manager, then follow from step 2.

```bash
# Fedora
sudo dnf install git python3 pipx

# Arch
sudo pacman -S git python python-pipx
```

If your distribution does not package pipx:

```bash
python3 -m pip install --user pipx
python3 -m pipx ensurepath
```

---

## 9. Troubleshooting

### `apio: command not found` right after installing

Open a new terminal. `pipx ensurepath` edits your shell profile, which only
takes effect in a new shell.

### `pip install` reports "externally-managed-environment"

Newer distributions stop pip from writing into the system Python. Install pipx
through your package manager instead (`sudo apt install pipx`), which is the
first step above.

### apio upload cannot find the board

Work through these in order:

1. Confirm the board is connected: `lsusb | grep 2e8a` should list a device.
   Nothing there usually means a power-only USB cable.
2. If you have not done step 5, do it and replug the board.
3. For a board that has never been flashed, hold BOOTSEL while plugging it in.

### apio upload mentions picotool and permissions

You can ignore this. `picotool` is an optional accelerated path. apio falls
back to copying the firmware onto the board's `RPI-RP2` drive and reports
`Upload complete` when it succeeds.
