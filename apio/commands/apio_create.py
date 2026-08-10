# -*- coding: utf-8 -*-
# -- This file is part of the Apio project
# -- (C) 2016-2024 FPGAwars
# -- Authors
# --  * Jesús Arroyo (2016-2019)
# --  * Juan Gonzalez (obijuan) (2019-2024)
# -- License GPLv2
"""Implementation of 'apio create' command"""

import sys
from typing import Optional
from pathlib import Path
import click
from apio.common.apio_console import cerror
from apio.utils import util, cmd_util
from apio.commands import options
from apio.apio_context import (
    ApioContext,
    PackagesPolicy,
    ProjectPolicy,
    RemoteConfigPolicy,
)
from apio.managers.project import (
    DEFAULT_TOP_MODULE,
    create_project_file,
)


board_option = click.option(
    "board",  # Var name.
    "-b",
    "--board",
    type=str,
    required=False,
    metavar="BOARD",
    help=(
        "Set the board. If omitted, choose Pico or UPduino 3.1 "
        "interactively."
    ),
    cls=cmd_util.ApioOption,
)


CLASS_BOARD_CHOICES = {
    "1": ("pico", "Raspberry Pi Pico"),
    "2": ("upduino31", "UPduino 3.1"),
}


def _prompt_for_board() -> str:
    """Prompt for one of the two boards used by the class."""

    click.echo("Select a board:")
    for number, (_, label) in CLASS_BOARD_CHOICES.items():
        click.echo(f"  {number}. {label}")
    selection = click.prompt(
        "Board",
        type=click.Choice(tuple(CLASS_BOARD_CHOICES), case_sensitive=True),
        show_choices=False,
    )
    return CLASS_BOARD_CHOICES[selection][0]


# -------------- apio create

# -- Text in the rich-text format of the python rich library.
APIO_CREATE_HELP = """
The command 'apio create' creates a new 'apio.ini' project file and is \
typically used when setting up a new Apio project.

Examples:[code]
  apio create
  apio create --board pico
  apio create --board upduino31 --top-module MyModule[/code]

[b][NOTE][/b] This command only creates a new 'apio.ini' file, rather than a \
complete and buildable project. To create complete projects, refer to the \
'apio examples' command.
"""


@click.command(
    name="create",
    cls=cmd_util.ApioCommand,
    short_help="Create an apio.ini project file.",
    help=APIO_CREATE_HELP,
)
@click.pass_context
@board_option
@options.top_module_option_gen(short_help="Set the top level module name.")
@options.project_dir_option
def cli(
    _: click.Context,
    *,
    # Options
    board: Optional[str],
    top_module: str,
    project_dir: Optional[Path],
):
    """Create a project file."""

    if board is None:
        board = _prompt_for_board()

    if not top_module:
        top_module = DEFAULT_TOP_MODULE

    # -- Create the apio context.
    apio_ctx = ApioContext(
        project_policy=ProjectPolicy.NO_PROJECT,
        remote_config_policy=RemoteConfigPolicy.OFFLINE_OK,
        packages_policy=PackagesPolicy.ENSURE_PACKAGES,
    )

    # -- Make sure the board exists.
    if board not in apio_ctx.boards:
        cerror(f"Unknown board id '{board}'.")
        sys.exit(1)

    # -- Determine the new project directory. Create if needed.
    project_dir_new: Path = util.user_directory_or_cwd(
        project_dir, description="Project", create_if_missing=True
    )

    # Create the apio.ini file. It exists with an error status if any error.
    create_project_file(
        project_dir_new,
        board,
        top_module,
    )
