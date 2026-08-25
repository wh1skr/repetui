"""Persistent, template-scoped presentation preferences."""

from __future__ import annotations

import json
import os
from enum import Enum
from pathlib import Path
from typing import Protocol

from .presentation import CardTemplateIdentity


class SectionMode(str, Enum):
    """How one back section participates in the review flow."""

    SHOW = "show"
    FOLD = "fold"
    HIDE = "hide"

    @property
    def next(self) -> SectionMode:
        order = (SectionMode.SHOW, SectionMode.FOLD, SectionMode.HIDE)
        return order[(order.index(self) + 1) % len(order)]


class Preferences(Protocol):
    """Small injectable seam used by the review and settings screens."""

    def mode(self, identity: CardTemplateIdentity, section_id: str) -> SectionMode: ...

    def set_mode(
        self,
        identity: CardTemplateIdentity,
        section_id: str,
        mode: SectionMode,
    ) -> None: ...


def default_preferences_path() -> Path:
    """Return the XDG-compliant user preference file."""
    config_home = os.environ.get("XDG_CONFIG_HOME")
    root = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    return root / "repetui" / "preferences.json"


class SectionPreferences:
    """JSON-backed preferences keyed by stable Anki model and template IDs."""

    VERSION = 1

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_preferences_path()
        self._templates = self._load()

    @staticmethod
    def _template_key(identity: CardTemplateIdentity) -> str:
        return f"{identity.note_type_id}:{identity.template_ordinal}"

    def _load(self) -> dict[str, dict[str, object]]:
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(document, dict) or document.get("version") != self.VERSION:
            return {}
        templates = document.get("templates")
        if not isinstance(templates, dict):
            return {}
        return {
            str(key): value
            for key, value in templates.items()
            if isinstance(value, dict)
        }

    def mode(self, identity: CardTemplateIdentity, section_id: str) -> SectionMode:
        template = self._templates.get(self._template_key(identity), {})
        sections = template.get("sections", {})
        if not isinstance(sections, dict):
            return SectionMode.SHOW
        try:
            return SectionMode(sections.get(section_id, SectionMode.SHOW.value))
        except ValueError:
            return SectionMode.SHOW

    def set_mode(
        self,
        identity: CardTemplateIdentity,
        section_id: str,
        mode: SectionMode,
    ) -> None:
        key = self._template_key(identity)
        template = self._templates.setdefault(
            key,
            {
                "note_type_name": identity.note_type_name,
                "template_name": identity.template_name,
                "sections": {},
            },
        )
        sections = template.setdefault("sections", {})
        if not isinstance(sections, dict):
            sections = {}
            template["sections"] = sections
        if mode is SectionMode.SHOW:
            sections.pop(section_id, None)
        else:
            sections[section_id] = mode.value
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        document = {"version": self.VERSION, "templates": self._templates}
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temporary.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)
