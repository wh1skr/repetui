"""Persistent profile and card-template preferences."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from enum import Enum
from pathlib import Path
from typing import Protocol

from .config import ProfilePaths
from .controls import ReviewControls
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


class AnswerLayout(str, Enum):
    """How short labelled answers share the compact review surface."""

    COMPACT = "compact"
    STACKED = "stacked"

    @property
    def next(self) -> AnswerLayout:
        return (
            AnswerLayout.STACKED
            if self is AnswerLayout.COMPACT
            else AnswerLayout.COMPACT
        )


class Preferences(Protocol):
    """Small injectable seam used by deck, review, and settings screens."""

    def expanded_deck_ids(self, profile: ProfilePaths) -> frozenset[int]: ...

    def set_deck_expanded(
        self, profile: ProfilePaths, deck_id: int, *, expanded: bool
    ) -> None: ...

    def review_controls(self, profile: ProfilePaths) -> ReviewControls: ...

    def set_review_controls(
        self, profile: ProfilePaths, controls: ReviewControls
    ) -> None: ...

    def add_on_enabled(self, profile: ProfilePaths, add_on_id: str) -> bool: ...

    def set_add_on_enabled(
        self, profile: ProfilePaths, add_on_id: str, *, enabled: bool
    ) -> None: ...

    def add_on_settings(
        self, profile: ProfilePaths, add_on_id: str
    ) -> dict[str, str | int | bool]: ...

    def set_add_on_setting(
        self,
        profile: ProfilePaths,
        add_on_id: str,
        setting_id: str,
        value: str | int | bool,
    ) -> None: ...

    def mode(self, identity: CardTemplateIdentity, section_id: str) -> SectionMode: ...

    def set_mode(
        self,
        identity: CardTemplateIdentity,
        section_id: str,
        mode: SectionMode,
    ) -> None: ...

    def answer_layout(self, identity: CardTemplateIdentity) -> AnswerLayout: ...

    def set_answer_layout(
        self,
        identity: CardTemplateIdentity,
        layout: AnswerLayout,
    ) -> None: ...


def default_preferences_path() -> Path:
    """Return the XDG-compliant user preference file."""
    config_home = os.environ.get("XDG_CONFIG_HOME")
    root = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    return root / "repetui" / "preferences.json"


class JsonPreferences:
    """JSON-backed preferences for profile and card-template behavior."""

    VERSION = 1

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_preferences_path()
        self._templates, self._profiles = self._load()

    @staticmethod
    def _template_key(identity: CardTemplateIdentity) -> str:
        return f"{identity.note_type_id}:{identity.template_ordinal}"

    @staticmethod
    def _profile_key(profile: ProfilePaths) -> str:
        return str(profile.collection.expanduser().resolve())

    def _load(
        self,
    ) -> tuple[dict[str, dict[str, object]], dict[str, dict[str, object]]]:
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}, {}
        if not isinstance(document, dict) or document.get("version") != self.VERSION:
            return {}, {}
        templates = document.get("templates")
        profiles = document.get("profiles")
        loaded_templates = {
            str(key): value
            for key, value in (templates.items() if isinstance(templates, dict) else ())
            if isinstance(value, dict)
        }
        loaded_profiles = {
            str(key): value
            for key, value in (profiles.items() if isinstance(profiles, dict) else ())
            if isinstance(value, dict)
        }
        return loaded_templates, loaded_profiles

    def expanded_deck_ids(self, profile: ProfilePaths) -> frozenset[int]:
        saved_profile = self._profiles.get(self._profile_key(profile), {})
        saved_ids = saved_profile.get("expanded_deck_ids", [])
        if not isinstance(saved_ids, list):
            return frozenset()
        return frozenset(
            deck_id
            for deck_id in saved_ids
            if isinstance(deck_id, int)
            and not isinstance(deck_id, bool)
            and deck_id > 0
        )

    def set_deck_expanded(
        self, profile: ProfilePaths, deck_id: int, *, expanded: bool
    ) -> None:
        expanded_ids = set(self.expanded_deck_ids(profile))
        if expanded:
            expanded_ids.add(deck_id)
        else:
            expanded_ids.discard(deck_id)
        saved_profile = self._profiles.setdefault(
            self._profile_key(profile), {"name": profile.name}
        )
        saved_profile["expanded_deck_ids"] = sorted(expanded_ids)
        self._save()

    def review_controls(self, profile: ProfilePaths) -> ReviewControls:
        saved_profile = self._profiles.get(self._profile_key(profile), {})
        return ReviewControls.from_saved(saved_profile.get("review_controls", {}))

    def set_review_controls(
        self, profile: ProfilePaths, controls: ReviewControls
    ) -> None:
        profiles = {key: dict(value) for key, value in self._profiles.items()}
        profile_key = self._profile_key(profile)
        saved_profile = profiles.setdefault(profile_key, {"name": profile.name})
        overrides = controls.saved_overrides()
        if overrides:
            saved_profile["review_controls"] = overrides
        else:
            saved_profile.pop("review_controls", None)
        self._write_document(self._templates, profiles)
        self._profiles = profiles

    def add_on_enabled(self, profile: ProfilePaths, add_on_id: str) -> bool:
        return self._add_on_state(profile, add_on_id).get("enabled") is True

    def set_add_on_enabled(
        self, profile: ProfilePaths, add_on_id: str, *, enabled: bool
    ) -> None:
        def update(state: dict[str, object]) -> None:
            if enabled:
                state["enabled"] = True
            else:
                state.pop("enabled", None)

        self._update_add_on_state(profile, add_on_id, update)

    def _add_on_state(
        self, profile: ProfilePaths, add_on_id: str
    ) -> dict[str, object]:
        saved_profile = self._profiles.get(self._profile_key(profile), {})
        add_ons = saved_profile.get("add_ons", {})
        if not isinstance(add_ons, dict):
            return {}
        state = add_ons.get(add_on_id, {})
        return state if isinstance(state, dict) else {}

    def _update_add_on_state(
        self,
        profile: ProfilePaths,
        add_on_id: str,
        update: Callable[[dict[str, object]], None],
    ) -> None:
        profiles = {key: dict(value) for key, value in self._profiles.items()}
        profile_key = self._profile_key(profile)
        saved_profile = profiles.setdefault(profile_key, {"name": profile.name})
        add_ons = dict(
            saved_profile.get("add_ons", {})
            if isinstance(saved_profile.get("add_ons"), dict)
            else {}
        )
        state = dict(
            add_ons.get(add_on_id, {})
            if isinstance(add_ons.get(add_on_id), dict)
            else {}
        )
        update(state)
        if state:
            add_ons[add_on_id] = state
        else:
            add_ons.pop(add_on_id, None)
        if add_ons:
            saved_profile["add_ons"] = add_ons
        else:
            saved_profile.pop("add_ons", None)
        self._write_document(self._templates, profiles)
        self._profiles = profiles

    def add_on_settings(
        self, profile: ProfilePaths, add_on_id: str
    ) -> dict[str, str | int | bool]:
        state = self._add_on_state(profile, add_on_id)
        settings = state.get("settings", {})
        if not isinstance(settings, dict):
            return {}
        return {
            str(key): value
            for key, value in settings.items()
            if isinstance(value, (str, int, bool))
        }

    def set_add_on_setting(
        self,
        profile: ProfilePaths,
        add_on_id: str,
        setting_id: str,
        value: str | int | bool,
    ) -> None:
        def update(state: dict[str, object]) -> None:
            settings = dict(
                state.get("settings", {})
                if isinstance(state.get("settings"), dict)
                else {}
            )
            settings[setting_id] = value
            state["settings"] = settings

        self._update_add_on_state(profile, add_on_id, update)

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

    def answer_layout(self, identity: CardTemplateIdentity) -> AnswerLayout:
        template = self._templates.get(self._template_key(identity), {})
        try:
            return AnswerLayout(
                template.get("answer_layout", AnswerLayout.COMPACT.value)
            )
        except (TypeError, ValueError):
            return AnswerLayout.COMPACT

    def set_answer_layout(
        self,
        identity: CardTemplateIdentity,
        layout: AnswerLayout,
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
        if layout is AnswerLayout.COMPACT:
            template.pop("answer_layout", None)
        else:
            template["answer_layout"] = layout.value
        self._save()

    def _save(self) -> None:
        self._write_document(self._templates, self._profiles)

    def _write_document(
        self,
        templates: dict[str, dict[str, object]],
        profiles: dict[str, dict[str, object]],
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        document = {
            "version": self.VERSION,
            "profiles": profiles,
            "templates": templates,
        }
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temporary.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)
