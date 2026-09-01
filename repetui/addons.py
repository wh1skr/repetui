"""Controlled first-party hooks for optional presentation behavior."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

from .config import ProfilePaths
from .preferences import Preferences


class AddOnEventType(str, Enum):
    """Review lifecycle events that official add-ons may observe."""

    REVIEW_STARTED = "review.started"
    RATING_ACCEPTED = "review.rating-accepted"
    REVIEW_COMPLETED = "review.completed"


@dataclass(frozen=True)
class AddOnEvent:
    """Presentation-safe facts about one review lifecycle transition."""

    type: AddOnEventType
    deck_name: str = ""
    rating: int | None = None


class PresentationCueType(str, Enum):
    """Presentation forms implemented by repetui itself."""

    NOTICE = "notice"
    COMPLETION_CELEBRATION = "completion-celebration"


@dataclass(frozen=True)
class PresentationCue:
    """A request for the app to present feedback without exposing Anki state."""

    type: PresentationCueType
    message: str = ""
    values: Mapping[str, str | int | bool] | None = None


SettingValue = str | int | bool
AddOnHandler = Callable[[AddOnEvent, Mapping[str, SettingValue]], PresentationCue | None]


@dataclass(frozen=True)
class ToggleSetting:
    id: str
    label: str
    default: bool = False

    def accepts(self, value: object) -> bool:
        return isinstance(value, bool)

    def next_value(self, value: SettingValue) -> bool:
        return not value if isinstance(value, bool) else self.default


@dataclass(frozen=True)
class ChoiceSetting:
    id: str
    label: str
    choices: tuple[str, ...]
    default: str

    def accepts(self, value: object) -> bool:
        return isinstance(value, str) and value in self.choices

    def next_value(self, value: SettingValue) -> str:
        current = value if self.accepts(value) else self.default
        return self.choices[(self.choices.index(current) + 1) % len(self.choices)]


@dataclass(frozen=True)
class NumberSetting:
    id: str
    label: str
    minimum: int
    maximum: int
    step: int
    default: int

    def accepts(self, value: object) -> bool:
        return (
            isinstance(value, int)
            and not isinstance(value, bool)
            and self.minimum <= value <= self.maximum
            and (value - self.minimum) % self.step == 0
        )

    def next_value(self, value: SettingValue) -> int:
        current = value if self.accepts(value) else self.default
        assert isinstance(current, int) and not isinstance(current, bool)
        candidate = current + self.step
        return self.minimum if candidate > self.maximum else candidate


SettingDefinition = ToggleSetting | ChoiceSetting | NumberSetting


@dataclass(frozen=True)
class AddOnDefinition:
    """Metadata and controlled event handler for one bundled add-on."""

    id: str
    name: str
    description: str
    events: frozenset[AddOnEventType]
    settings: tuple[SettingDefinition, ...]
    handle: AddOnHandler

    def __post_init__(self) -> None:
        if (
            re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", self.id) is None
            or not self.name.strip()
            or not self.description.strip()
            or not self.events
            or not all(isinstance(event, AddOnEventType) for event in self.events)
        ):
            raise ValueError("Invalid add-on definition metadata")
        setting_ids = [setting.id for setting in self.settings]
        if len(setting_ids) != len(set(setting_ids)):
            raise ValueError("Invalid setting definition: duplicate ID")
        for setting in self.settings:
            valid = bool(setting.id and setting.label)
            if isinstance(setting, ChoiceSetting):
                valid = (
                    valid
                    and bool(setting.choices)
                    and len(setting.choices) == len(set(setting.choices))
                    and setting.default in setting.choices
                )
            elif isinstance(setting, NumberSetting):
                valid = (
                    valid
                    and setting.step > 0
                    and setting.minimum <= setting.maximum
                    and setting.minimum <= setting.default <= setting.maximum
                    and (setting.default - setting.minimum) % setting.step == 0
                )
            if not valid:
                raise ValueError(f"Invalid setting definition: {setting.id}")


@dataclass(frozen=True)
class DispatchFailure:
    add_on_id: str


@dataclass(frozen=True)
class DispatchReport:
    cues: tuple[PresentationCue, ...] = ()
    failures: tuple[DispatchFailure, ...] = ()


class AddOnManager:
    """Own registration, profile state, and failure-contained event delivery."""

    def __init__(
        self,
        definitions: Sequence[AddOnDefinition],
        preferences: Preferences,
        profile: ProfilePaths,
    ) -> None:
        self.definitions = tuple(definitions)
        ids = [definition.id for definition in self.definitions]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate add-on ID")
        self.preferences = preferences
        self.profile = profile

    def is_enabled(self, add_on_id: str) -> bool:
        return self.preferences.add_on_enabled(self.profile, add_on_id)

    def set_enabled(self, add_on_id: str, enabled: bool) -> None:
        self.preferences.set_add_on_enabled(
            self.profile, add_on_id, enabled=enabled
        )

    def _definition(self, add_on_id: str) -> AddOnDefinition:
        for definition in self.definitions:
            if definition.id == add_on_id:
                return definition
        raise KeyError(add_on_id)

    def _setting_definition(
        self, add_on_id: str, setting_id: str
    ) -> SettingDefinition:
        definition = self._definition(add_on_id)
        setting = next(
            (setting for setting in definition.settings if setting.id == setting_id),
            None,
        )
        if setting is None:
            raise KeyError(setting_id)
        return setting

    def setting_values(self, add_on_id: str) -> dict[str, SettingValue]:
        definition = self._definition(add_on_id)
        saved = self.preferences.add_on_settings(self.profile, add_on_id)
        return {
            setting.id: (
                saved[setting.id]
                if setting.id in saved and setting.accepts(saved[setting.id])
                else setting.default
            )
            for setting in definition.settings
        }

    def set_setting(
        self, add_on_id: str, setting_id: str, value: SettingValue
    ) -> None:
        setting = self._setting_definition(add_on_id, setting_id)
        if not setting.accepts(value):
            raise ValueError(f"Invalid value for {add_on_id}.{setting_id}")
        self.preferences.set_add_on_setting(
            self.profile, add_on_id, setting_id, value
        )

    def cycle_setting(self, add_on_id: str, setting_id: str) -> SettingValue:
        setting = self._setting_definition(add_on_id, setting_id)
        value = setting.next_value(self.setting_values(add_on_id)[setting_id])
        self.set_setting(add_on_id, setting_id, value)
        return value

    def dispatch(self, event: AddOnEvent) -> DispatchReport:
        cues: list[PresentationCue] = []
        failures: list[DispatchFailure] = []
        for definition in self.definitions:
            if event.type not in definition.events or not self.is_enabled(definition.id):
                continue
            try:
                cue = definition.handle(event, self.setting_values(definition.id))
            except Exception:
                failures.append(DispatchFailure(definition.id))
                continue
            if cue is not None:
                cues.append(cue)
        return DispatchReport(tuple(cues), tuple(failures))


def bundled_add_ons() -> tuple[AddOnDefinition, ...]:
    """Return the official add-ons shipped with this repetui build."""
    from .completion import completion_celebration_add_on

    return (completion_celebration_add_on(),)
