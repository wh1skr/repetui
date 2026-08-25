"""Command-line entry point for the repetui application."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .app import RepetuiApp
from .backend import AnkiBackend
from .config import ProfileNotFoundError, discover_profile


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        prog="repetui",
        description="Review an existing Anki collection without leaving the terminal.",
    )
    command.add_argument("--profile", help="Anki profile name")
    command.add_argument("--collection", type=Path, help="explicit collection.anki2 path")
    command.add_argument("--version", action="version", version=f"repetui {__version__}")
    return command


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        profile = discover_profile(args.profile, args.collection)
    except ProfileNotFoundError as exc:
        print(f"repetui: {exc}", file=sys.stderr)
        return 2

    RepetuiApp(AnkiBackend(profile.collection), profile).run()
    return 0

