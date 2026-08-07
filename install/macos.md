# Installing apio on macOS

> **Not yet tested.** The pico target has been verified end to end on Linux and
> Windows, but not on macOS. These instructions are the expected procedure
> rather than a confirmed one; please report anything that does not work.

Everything here is done from **Terminal** with [Homebrew](https://brew.sh),
the standard macOS package manager. Nothing needs downloading by hand.

If you do not already have Homebrew, install it with the one command from
[brew.sh](https://brew.sh), then follow its instructions to add `brew` to your
`PATH`.

Check it:

```bash
brew --version
```

---

## 1. Install Git, Python and pipx

```bash
brew install git python pipx
pipx ensurepath
```

apio needs Python 3.10 or newer; Homebrew's `python` is well beyond that.
Check with:

```bash
python3 --version
```

`pipx ensurepath` updates your shell profile. **Close Terminal and open a new
window** so the change takes effect.

## 2. Clone and install apio

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

## 3. Install the toolchain

Unlike upstream apio, this fork never downloads packages on its own. Fetch them
once, explicitly:

```bash
apio packages install
```

This pulls yosys, the ARM compiler, CMake, Ninja, the Pico SDK and TinyUSB.
It is a large download and takes several minutes. Re-run it after any
`git pull` that changes the package definitions.

## 4. Check it works

```bash
cd ~/apio/experimental/pico-hello
apio build
```

A successful build ends with `[SUCCESS]`.

---

## Connecting a Pico

No drivers or permission changes should be needed. macOS recognises both states
of the board on its own: a serial port when it is running your firmware, and a
volume named `RPI-RP2` when it is in its bootloader.

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

## Troubleshooting

### `apio: command not found` right after installing

Open a new Terminal window. `pipx ensurepath` edits your shell profile, which
only takes effect in a new shell.

### macOS refuses to run a downloaded tool

apio's toolchain is fetched from each project's official releases rather than
installed through Homebrew, so Gatekeeper may quarantine some of it. If a build
fails with a message about an unidentified developer or a damaged binary,
please report it along with the exact wording — this is one of the parts most
likely to need adjusting for macOS.

### apio upload cannot find the board

Work through these in order:

1. Confirm the board is connected — it should appear in
   `ls /dev/tty.usbmodem*` when running firmware, or as a volume at
   `/Volumes/RPI-RP2` in bootloader mode. Neither appearing usually means a
   power-only USB cable.
2. For a board that has never been flashed, hold BOOTSEL while plugging it in.
