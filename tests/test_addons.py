from pathlib import Path

import pytest

from repetui.addons import (
    AddOnDefinition,
    AddOnEvent,
    AddOnEventType,
    AddOnManager,
    ChoiceSetting,
    DispatchFailure,
    NumberSetting,
    PresentationCue,
    PresentationCueType,
    ToggleSetting,
)
from repetui.config import ProfilePaths
from repetui.preferences import JsonPreferences


def profile(base: Path, name: str = "whskr") -> ProfilePaths:
    return ProfilePaths(base, name, base / name / "collection.anki2")


def test_registered_add_on_is_visible_but_disabled_until_profile_enables_it(
    tmp_path,
) -> None:
    handled: list[AddOnEvent] = []

    def handle(event, _settings):
        handled.append(event)
        return PresentationCue(PresentationCueType.NOTICE, "complete")

    definition = AddOnDefinition(
        id="completion-celebration",
        name="Completion Celebration",
        description="Celebrate the final due card.",
        events=frozenset({AddOnEventType.REVIEW_COMPLETED}),
        settings=(),
        handle=handle,
    )
    manager = AddOnManager(
        (definition,),
        JsonPreferences(tmp_path / "preferences.json"),
        profile(tmp_path / "Anki2"),
    )

    assert manager.definitions == (definition,)
    assert manager.is_enabled(definition.id) is False
    assert manager.dispatch(AddOnEvent(AddOnEventType.REVIEW_COMPLETED)).cues == ()
    assert handled == []


def test_enabled_state_survives_restart_and_is_isolated_by_profile(tmp_path) -> None:
    definition = AddOnDefinition(
        id="completion-celebration",
        name="Completion Celebration",
        description="Celebrate the final due card.",
        events=frozenset({AddOnEventType.REVIEW_COMPLETED}),
        settings=(),
        handle=lambda _event, _settings: PresentationCue(
            PresentationCueType.NOTICE, "complete"
        ),
    )
    path = tmp_path / "preferences.json"
    personal = profile(tmp_path / "Anki2")
    work = profile(tmp_path / "Anki2", "work")

    AddOnManager((definition,), JsonPreferences(path), personal).set_enabled(
        definition.id, True
    )

    restarted = AddOnManager((definition,), JsonPreferences(path), personal)
    other_profile = AddOnManager((definition,), JsonPreferences(path), work)
    assert restarted.is_enabled(definition.id) is True
    assert restarted.dispatch(
        AddOnEvent(AddOnEventType.REVIEW_COMPLETED)
    ).cues == (PresentationCue(PresentationCueType.NOTICE, "complete"),)
    assert other_profile.is_enabled(definition.id) is False


def test_declarative_toggle_choice_and_number_settings_persist_valid_values(
    tmp_path,
) -> None:
    definition = AddOnDefinition(
        id="completion-celebration",
        name="Completion Celebration",
        description="Celebrate the final due card.",
        events=frozenset({AddOnEventType.REVIEW_COMPLETED}),
        settings=(
            ToggleSetting("sparkles", "Sparkles", default=True),
            ChoiceSetting("duration", "Duration", ("short", "medium", "long"), "short"),
            NumberSetting("density", "Density", minimum=1, maximum=5, step=2, default=1),
        ),
        handle=lambda _event, settings: PresentationCue(
            PresentationCueType.NOTICE, "complete", settings
        ),
    )
    path = tmp_path / "preferences.json"
    personal = profile(tmp_path / "Anki2")
    manager = AddOnManager((definition,), JsonPreferences(path), personal)

    assert manager.setting_values(definition.id) == {
        "sparkles": True,
        "duration": "short",
        "density": 1,
    }

    manager.set_setting(definition.id, "sparkles", False)
    manager.set_setting(definition.id, "duration", "long")
    manager.set_setting(definition.id, "density", 5)
    manager.set_enabled(definition.id, True)

    restarted = AddOnManager((definition,), JsonPreferences(path), personal)
    event = AddOnEvent(AddOnEventType.REVIEW_COMPLETED)
    assert restarted.setting_values(definition.id) == {
        "sparkles": False,
        "duration": "long",
        "density": 5,
    }
    assert restarted.dispatch(event).cues == (
        PresentationCue(
            PresentationCueType.NOTICE,
            "complete",
            {"sparkles": False, "duration": "long", "density": 5},
        ),
    )

    restarted.set_enabled(definition.id, False)
    disabled = AddOnManager((definition,), JsonPreferences(path), personal)
    assert disabled.is_enabled(definition.id) is False
    assert disabled.setting_values(definition.id)["duration"] == "long"


def test_duplicate_registration_ids_are_rejected(tmp_path) -> None:
    definition = AddOnDefinition(
        id="completion-celebration",
        name="Completion Celebration",
        description="Celebrate the final due card.",
        events=frozenset({AddOnEventType.REVIEW_COMPLETED}),
        settings=(),
        handle=lambda _event, _settings: None,
    )

    with pytest.raises(ValueError, match="Duplicate add-on ID"):
        AddOnManager(
            (definition, definition),
            JsonPreferences(tmp_path / "preferences.json"),
            profile(tmp_path / "Anki2"),
        )


@pytest.mark.parametrize(
    "setting",
    (
        ChoiceSetting("duration", "Duration", (), "short"),
        ChoiceSetting("duration", "Duration", ("short",), "long"),
        NumberSetting("density", "Density", 5, 1, 1, 1),
        NumberSetting("density", "Density", 1, 5, 0, 1),
    ),
)
def test_invalid_declarative_setting_definition_is_rejected(setting) -> None:
    with pytest.raises(ValueError, match="Invalid setting definition"):
        AddOnDefinition(
            id="completion-celebration",
            name="Completion Celebration",
            description="Celebrate the final due card.",
            events=frozenset({AddOnEventType.REVIEW_COMPLETED}),
            settings=(setting,),
            handle=lambda _event, _settings: None,
        )


@pytest.mark.parametrize(
    "overrides",
    (
        {"id": ""},
        {"id": "Completion Celebration"},
        {"name": ""},
        {"description": ""},
        {"events": frozenset()},
    ),
)
def test_add_on_registration_requires_stable_readable_metadata(overrides) -> None:
    values = {
        "id": "completion-celebration",
        "name": "Completion Celebration",
        "description": "Celebrate the final due card.",
        "events": frozenset({AddOnEventType.REVIEW_COMPLETED}),
    }
    values.update(overrides)

    with pytest.raises(ValueError, match="Invalid add-on definition"):
        AddOnDefinition(
            **values,
            settings=(),
            handle=lambda _event, _settings: None,
        )


def test_one_add_on_failure_does_not_interrupt_other_presentation_handlers(
    tmp_path,
) -> None:
    def fail(_event, _settings):
        raise RuntimeError("private failure detail")

    broken = AddOnDefinition(
        id="broken-feedback",
        name="Broken Feedback",
        description="A failing test add-on.",
        events=frozenset({AddOnEventType.RATING_ACCEPTED}),
        settings=(),
        handle=fail,
    )
    healthy = AddOnDefinition(
        id="healthy-feedback",
        name="Healthy Feedback",
        description="A healthy test add-on.",
        events=frozenset({AddOnEventType.RATING_ACCEPTED}),
        settings=(),
        handle=lambda _event, _settings: PresentationCue(
            PresentationCueType.NOTICE, "acknowledged"
        ),
    )
    preferences = JsonPreferences(tmp_path / "preferences.json")
    manager = AddOnManager(
        (broken, healthy), preferences, profile(tmp_path / "Anki2")
    )
    manager.set_enabled(broken.id, True)
    manager.set_enabled(healthy.id, True)

    report = manager.dispatch(AddOnEvent(AddOnEventType.RATING_ACCEPTED, rating=3))

    assert report.cues == (
        PresentationCue(PresentationCueType.NOTICE, "acknowledged"),
    )
    assert report.failures == (DispatchFailure("broken-feedback"),)
