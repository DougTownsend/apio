# Apio create

---

## apio create

The `apio create` command initializes a new `apio.ini` file. Use it to
start a new Apio project.

This command only generates a new `apio.ini` file. To create a full,
  buildable project, use `apio examples` to fetch a template for your board.

<h3>Examples</h3>

```
apio create
apio create --board pico
apio create --board upduino31 --top-module MyModule
```

When `--board` is omitted, Apio prompts you to select Raspberry Pi Pico
or UPduino 3.1 by number. Other supported boards can still be specified
explicitly with `--board`.

<h3>Options</h3>

```
-b, --board BOARD        Set the board. If omitted, choose Pico or UPduino 3.1 interactively.
-t, --top-module name    Set the top-level module name.
-p, --project-dir path   Specify the project root directory.
-h, --help               Show help message and exit.
```

