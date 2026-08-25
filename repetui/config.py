"""Locate an existing Anki profile without importing Anki's Qt interface."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ProfileNotFoundError(RuntimeError):
    """Raised when repetui cannot select an Anki profile safely."""


@dataclass(frozen=True)
class ProfilePaths:
    """Filesystem locations belonging to one Anki profile."""

    base: Path
    name: str
    collection: Path


def default_anki_base() -> Path:
    """Return Anki's data directory, allowing an explicit override."""
    override = os.environ.get("REPETUI_ANKI_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".local" / "share" / "Anki2"


def discover_profile(
    requested_profile: str | None = None,
    collection_override: str | Path | None = None,
) -> ProfilePaths:
    """Resolve one local profile or explain how to disambiguate it."""
    if collection_override is not None:
        collection = Path(collection_override).expanduser().resolve()
        if not collection.is_file():
            raise ProfileNotFoundError(f"Collection not found: {collection}")
        return ProfilePaths(
            base=collection.parent.parent,
            name=requested_profile or collection.parent.name,
            collection=collection,
        )

    base = default_anki_base()
    if not base.is_dir():
        raise ProfileNotFoundError(
            f"Anki data directory not found: {base}\n"
            "Open Anki Desktop once, or set REPETUI_ANKI_HOME."
        )

    if requested_profile:
        collection = base / requested_profile / "collection.anki2"
        if not collection.is_file():
            raise ProfileNotFoundError(
                f"Profile '{requested_profile}' has no collection at {collection}"
            )
        return ProfilePaths(base=base, name=requested_profile, collection=collection)

    candidates = sorted(
        path.parent.name for path in base.glob("*/collection.anki2") if path.is_file()
    )
    if not candidates:
        raise ProfileNotFoundError(f"No Anki profiles with a collection found in {base}")
    if len(candidates) > 1:
        choices = ", ".join(candidates)
        raise ProfileNotFoundError(
            f"Multiple Anki profiles found: {choices}\n"
            "Choose one with: repetui --profile PROFILE_NAME"
        )

    name = candidates[0]
    return ProfilePaths(base=base, name=name, collection=base / name / "collection.anki2")

