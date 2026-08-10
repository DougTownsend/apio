# Installing apio on macOS

Everything here is done from **Terminal** with [Homebrew](https://brew.sh),
the standard macOS package manager. Nothing needs downloading by hand.

> **If you have installed apio before**, uninstall it first. If you have an old version installed elsewhere, it may take precedence over the version we're installing here, causing confusing errors. Check with `which apio` to see if an older version exists, and uninstall it before proceeding.

---

## 1. Install Homebrew

Run this command in Terminal:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

This may take a few minutes. When it finishes, it will print instructions.

## 2. Add Homebrew to your PATH

After Homebrew finishes installing, it prints instructions on how to add `brew` to your `PATH`. **You must do this step or `brew` commands will not work.**

**For Apple Silicon Macs**, add this line to your shell profile. Run:

```bash
echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile
```

**For Intel Macs**, add this line instead:

```bash
echo 'eval "$(/usr/local/bin/brew shellenv)"' >> ~/.zprofile
```

## 3. Close and reopen Terminal

**Close Terminal and open a new window.** The changes to your shell profile only take effect in a new shell.

Verify Homebrew is working:

```bash
brew --version
```

---

## 4. Install Git, Python and pipx

```bash
brew install git python pipx
pipx ensurepath
```

apio needs Python 3.10 or newer; Homebrew's `python` is well beyond that.
Check with:

```bash
python3 --version
```

## 5. Close and reopen Terminal again

**Close Terminal and open a new window.** The `pipx ensurepath` command updates your shell profile, and this change only takes effect in a new shell.

## 6. Clone and install apio

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

## 7. Install the toolchain

Unlike upstream apio, this fork never downloads packages on its own. Fetch them
once, explicitly:

```bash
apio packages install
```

This pulls yosys, the ARM compiler, CMake, Ninja, the Pico SDK and TinyUSB.
It is a large download and takes several minutes. Re-run it after any
`git pull` that changes the package definitions.

## 8. Check it works

```bash
cd ~/apio/experimental/pico-hello
apio build
```

A successful build ends with `[SUCCESS]`.

---

## 9. Connecting a Pico

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

## 10. Troubleshooting

### `apio: command not found` right after installing

Open a new Terminal window. `pipx ensurepath` edits your shell profile, which
only takes effect in a new shell.

### macOS refuses to run a downloaded tool

apio's toolchain is fetched from each project's official releases rather than
installed through Homebrew, so in principle Gatekeeper could quarantine some of
it. In testing it did not — `apio packages install` followed by `apio build`
worked with no Gatekeeper prompts. If a build does fail with a message about an
unidentified developer or a damaged binary, please report it along with the
exact wording.

### apio upload cannot find the board

Work through these in order:

1. Confirm the board is connected — it should appear in
   `ls /dev/tty.usbmodem*` when running firmware, or as a volume at
   `/Volumes/RPI-RP2` in bootloader mode. Neither appearing usually means a
   power-only USB cable.
2. For a board that has never been flashed, hold BOOTSEL while plugging it in.
