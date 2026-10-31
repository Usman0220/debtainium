"""
Debtainium Main Dispatcher
Dispatches between CLI and GUI modes based on command-line arguments.
"""

from __future__ import annotations

import sys
from typing import List, Optional

from debtainium.cli import run_cli
from debtainium.gui import run_gui


def main(args: Optional[List[str]] = None) -> int:
    """
    Main dispatch routine:
    - If no arguments provided: starts GUI
    - If first argument is 'gui': starts GUI
    - Otherwise: dispatches to CLI subcommands (add, remove, list, check, update, notify)
    """
    if args is None:
        args = sys.argv[1:]

    if not args:
        # Default with no arguments on desktop is GUI
        return run_gui()

    if args[0] == "gui":
        return run_gui(args[1:])

    return run_cli(args)


if __name__ == "__main__":
    sys.exit(main())
