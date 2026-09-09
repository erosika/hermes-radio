"""hermes-radio plugin. Registers the radio tools, ``/radio``, ``hermes radio``, and the daemon launcher."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Optional

from . import radio_tools as _tools
from .hermes_radio import client, paths
from .hermes_radio.commands import HELP, run as run_radio_command

logger = logging.getLogger(__name__)


def _hermes_root(ctx) -> Optional[str]:
    """The hermes-agent checkout the daemon imports for mic breaks. Optional; everything else works without it."""
    configured = ctx.get_config("hermes_root")
    if configured:
        return str(Path(str(configured)).expanduser())
    try:
        import hermes_constants
        return str(Path(hermes_constants.__file__).resolve().parent)
    except Exception:
        return None


def _setup_cli(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("words", nargs=argparse.REMAINDER, help="same grammar as /radio; try: hermes radio help")


def _cli_main(args: argparse.Namespace) -> int:
    words = [w for w in (getattr(args, "words", None) or []) if w != "--"]
    print(run_radio_command(" ".join(words)))
    return 0


def register(ctx) -> None:
    client.write_launcher(python=sys.executable, script=str(paths.daemon_script()), hermes_root=_hermes_root(ctx))
    for name, schema, handler in _tools.TOOLS:
        ctx.register_tool(name=name, toolset="radio", schema=schema, handler=handler,
                          check_fn=_tools.check_radio_available)
    ctx.register_command(
        "radio", run_radio_command,
        description="Hermes Radio: play, pause, skip, volume, record, search",
        args_hint="[play <station>|pause|skip|vol N|stop|help]", argument_mode="text")
    ctx.register_cli_command(
        name="radio", help="Hermes Radio player controls", setup_fn=_setup_cli, handler_fn=_cli_main,
        description="Control the Hermes Radio daemon from the shell.\n\n" + HELP)
    logger.debug("hermes-radio registered; launcher at %s", paths.launcher_path())
