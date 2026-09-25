import json
from pathlib import Path

import pytest

from repetui.config import ProfilePaths
from repetui.controls import ReviewAction, ReviewControls
from repetui.preferences import (
    ActionFeedbackDuration,
    AnswerLayout,
    JsonPreferences,
    SectionMode,
    default_preferences_path,
)
from repetui.presentation import CardTemplateIdentity, TemplateFieldProfile

JAPANESE_RECOGNITION = CardTemplateIdentity(204, "Japanese", 0, "Recognition")
JAPANESE_PRODUCTION = CardTemplateIdentity(204, "Japanese", 1, "Production")
AWS_BASIC = CardTemplateIdentity(305, "AWS", 0, "Basic")


def profile(base: Path, name: str = "whskr") -> ProfilePaths:
    return ProfilePaths(base, name, base / name / "collection.anki2")


def test_unknown_sections_default_to_show_without_writing_a_file(tmp_path) -> None:
    path = tmp_path / "preferences.json"
    preferences = JsonPreferences(path)

    assert preferences.mode(JAPANESE_RECOGNITION, "back:heading:mnemonic") is SectionMode.SHOW
    assert not path.exists()


def test_section_modes_survive_restart_and_are_scoped_to_template(tmp_path) -> None:
    path = tmp_path / "preferences.json"
    preferences = JsonPreferences(path)
    preferences.set_mode(
        JAPANESE_RECOGNITION,
        "back:heading:mnemonic",
        SectionMode.FOLD,
    )
    preferences.set_mode(
        JAPANESE_RECOGNITION,
        "back:heading:examples",
        SectionMode.HIDE,
    )

    restarted = JsonPreferences(path)

    assert restarted.mode(
        JAPANESE_RECOGNITION, "back:heading:mnemonic"
    ) is SectionMode.FOLD
    assert restarted.mode(
        JAPANESE_RECOGNITION, "back:heading:examples"
    ) is SectionMode.HIDE
    assert restarted.mode(
        JAPANESE_PRODUCTION, "back:heading:mnemonic"
    ) is SectionMode.SHOW
    assert restarted.mode(AWS_BASIC, "back:heading:mnemonic") is SectionMode.SHOW

    document = json.loads(path.read_text())
    assert document["templates"]["204:0"]["sections"] == {
        "back:heading:examples": "hide",
        "back:heading:mnemonic": "fold",
    }


def test_answer_layout_defaults_stacked_and_survives_restart_per_template(
    tmp_path,
) -> None:
    path = tmp_path / "preferences.json"
    preferences = JsonPreferences(path)

    assert preferences.answer_layout(JAPANESE_RECOGNITION) is AnswerLayout.STACKED
    assert not path.exists()

    preferences.set_answer_layout(JAPANESE_RECOGNITION, AnswerLayout.COMPACT)
    restarted = JsonPreferences(path)

    assert restarted.answer_layout(JAPANESE_RECOGNITION) is AnswerLayout.COMPACT
    assert restarted.answer_layout(JAPANESE_PRODUCTION) is AnswerLayout.STACKED
    assert restarted.answer_layout(AWS_BASIC) is AnswerLayout.STACKED
    document = json.loads(path.read_text())
    assert document["templates"]["204:0"]["answer_layout"] == "compact"

    restarted.set_answer_layout(JAPANESE_RECOGNITION, AnswerLayout.STACKED)
    assert "answer_layout" not in json.loads(path.read_text())["templates"]["204:0"]


@pytest.mark.parametrize("requested", [SectionMode.SHOW, SectionMode.HIDE])
def test_failed_section_mode_write_preserves_active_and_saved_choice(
    tmp_path, monkeypatch, requested
) -> None:
    path = tmp_path / "preferences.json"
    preferences = JsonPreferences(path)
    section = "back:heading:mnemonic"
    preferences.set_mode(JAPANESE_RECOGNITION, section, SectionMode.FOLD)

    def fail_replace(_source, _destination):
        raise OSError("disk unavailable")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="disk unavailable"):
        preferences.set_mode(JAPANESE_RECOGNITION, section, requested)

    assert preferences.mode(JAPANESE_RECOGNITION, section) is SectionMode.FOLD
    assert JsonPreferences(path).mode(JAPANESE_RECOGNITION, section) is SectionMode.FOLD


@pytest.mark.parametrize("original", list(AnswerLayout))
def test_failed_answer_layout_write_preserves_active_and_saved_choice(
    tmp_path, monkeypatch, original
) -> None:
    path = tmp_path / "preferences.json"
    preferences = JsonPreferences(path)
    preferences.set_answer_layout(JAPANESE_RECOGNITION, original)

    def fail_replace(_source, _destination):
        raise OSError("disk unavailable")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="disk unavailable"):
        preferences.set_answer_layout(JAPANESE_RECOGNITION, original.next)

    assert preferences.answer_layout(JAPANESE_RECOGNITION) is original
    assert JsonPreferences(path).answer_layout(JAPANESE_RECOGNITION) is original


@pytest.mark.parametrize("expanded", [False, True])
def test_failed_deck_expansion_write_preserves_active_and_saved_choice(
    tmp_path, monkeypatch, expanded
) -> None:
    path = tmp_path / "preferences.json"
    preferences = JsonPreferences(path)
    current_profile = profile(tmp_path)
    preferences.set_deck_expanded(current_profile, 10, expanded=not expanded)
    original = preferences.expanded_deck_ids(current_profile)

    def fail_replace(_source, _destination):
        raise OSError("disk unavailable")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="disk unavailable"):
        preferences.set_deck_expanded(current_profile, 10, expanded=expanded)

    assert preferences.expanded_deck_ids(current_profile) == original
    assert JsonPreferences(path).expanded_deck_ids(current_profile) == original


def test_existing_saved_stacked_answer_layout_remains_valid(tmp_path) -> None:
    path = tmp_path / "preferences.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "profiles": {},
                "templates": {
                    "204:0": {
                        "answer_layout": "stacked",
                        "note_type_name": "Japanese",
                        "sections": {},
                        "template_name": "Recognition",
                    }
                },
            }
        )
    )

    preferences = JsonPreferences(path)

    assert preferences.answer_layout(JAPANESE_RECOGNITION) is AnswerLayout.STACKED


def test_field_profile_survives_restart_with_order_and_template_scope(tmp_path) -> None:
    path = tmp_path / "preferences.json"
    preferences = JsonPreferences(path)
    configured = TemplateFieldProfile(
        prompt_fields=("Expression",),
        answer_fields=("Reading", "Meaning", "Example"),
        ignored_fields=("Private note",),
    )

    assert preferences.field_profile(JAPANESE_RECOGNITION) is None
    assert not path.exists()

    preferences.set_field_profile(JAPANESE_RECOGNITION, configured)
    restarted = JsonPreferences(path)

    assert restarted.field_profile(JAPANESE_RECOGNITION) == configured
    assert restarted.field_profile(JAPANESE_PRODUCTION) is None
    assert restarted.field_profile(AWS_BASIC) is None
    assert json.loads(path.read_text())["templates"]["204:0"]["field_profile"] == {
        "answer_fields": ["Reading", "Meaning", "Example"],
        "ignored_fields": ["Private note"],
        "prompt_fields": ["Expression"],
    }


def test_malformed_or_ambiguous_field_profiles_are_ignored_and_rejected(tmp_path) -> None:
    path = tmp_path / "preferences.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "profiles": {},
                "templates": {
                    "204:0": {
                        "field_profile": {
                            "prompt_fields": ["Expression", "Expression"],
                            "answer_fields": ["Meaning"],
                        }
                    }
                },
            }
        )
    )
    preferences = JsonPreferences(path)

    assert preferences.field_profile(JAPANESE_RECOGNITION) is None
    with pytest.raises(ValueError, match="unique prompt and answer"):
        preferences.set_field_profile(
            JAPANESE_RECOGNITION,
            TemplateFieldProfile(("Expression",), ("Expression", "Meaning")),
        )


def test_failed_field_profile_write_preserves_active_and_saved_mapping(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "preferences.json"
    preferences = JsonPreferences(path)
    original = TemplateFieldProfile(("Expression",), ("Meaning",))
    preferences.set_field_profile(JAPANESE_RECOGNITION, original)

    def fail_replace(_source, _destination):
        raise OSError("disk unavailable")

    monkeypatch.setattr(Path, "replace", fail_replace)

    with pytest.raises(OSError, match="disk unavailable"):
        preferences.set_field_profile(
            JAPANESE_RECOGNITION,
            TemplateFieldProfile(("Expression",), ("Meaning", "Example")),
        )

    assert preferences.field_profile(JAPANESE_RECOGNITION) == original
    assert JsonPreferences(path).field_profile(JAPANESE_RECOGNITION) == original


def test_default_path_respects_xdg_config_home(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

    assert default_preferences_path() == tmp_path / "xdg" / "repetui" / "preferences.json"


def test_deck_expansion_defaults_closed_and_survives_restart_per_profile(
    tmp_path,
) -> None:
    path = tmp_path / "preferences.json"
    preferences = JsonPreferences(path)
    whskr = profile(tmp_path / "Anki2")
    work = profile(tmp_path / "Anki2", "work")
    same_name_elsewhere = profile(tmp_path / "other-Anki2")

    assert preferences.expanded_deck_ids(whskr) == frozenset()
    assert not path.exists()

    preferences.set_deck_expanded(whskr, 101, expanded=True)
    preferences.set_deck_expanded(whskr, 202, expanded=True)

    restarted = JsonPreferences(path)
    assert restarted.expanded_deck_ids(whskr) == frozenset({101, 202})
    assert restarted.expanded_deck_ids(work) == frozenset()
    assert restarted.expanded_deck_ids(same_name_elsewhere) == frozenset()

    restarted.set_deck_expanded(whskr, 101, expanded=False)
    assert JsonPreferences(path).expanded_deck_ids(whskr) == frozenset({202})

    document = json.loads(path.read_text())
    saved = document["profiles"][str(whskr.collection.resolve())]
    assert saved == {"expanded_deck_ids": [202], "name": "whskr"}


def test_malformed_saved_deck_ids_are_ignored(tmp_path) -> None:
    path = tmp_path / "preferences.json"
    whskr = profile(tmp_path / "Anki2")
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "templates": {},
                "profiles": {
                    str(whskr.collection.resolve()): {
                        "expanded_deck_ids": [101, "missing", -1, 101]
                    },
                },
            }
        )
    )

    preferences = JsonPreferences(path)

    assert preferences.expanded_deck_ids(whskr) == frozenset({101})


def test_review_controls_survive_restart_and_are_isolated_by_profile(tmp_path) -> None:
    path = tmp_path / "preferences.json"
    whskr = profile(tmp_path / "Anki2")
    work = profile(tmp_path / "Anki2", "work")
    preferences = JsonPreferences(path)
    customized = ReviewControls.defaults().with_binding(ReviewAction.UNDO, "z")

    preferences.set_review_controls(whskr, customized)

    restarted = JsonPreferences(path)
    assert restarted.review_controls(whskr).binding(ReviewAction.UNDO) == "z"
    assert restarted.review_controls(work) == ReviewControls.defaults()


def test_action_feedback_duration_survives_restart_and_is_isolated_by_profile(
    tmp_path,
) -> None:
    path = tmp_path / "preferences.json"
    whskr = profile(tmp_path / "Anki2")
    work = profile(tmp_path / "Anki2", "work")
    preferences = JsonPreferences(path)

    assert (
        preferences.action_feedback_duration(whskr)
        is ActionFeedbackDuration.NORMAL
    )
    assert not path.exists()

    preferences.set_action_feedback_duration(whskr, ActionFeedbackDuration.BRIEF)
    restarted = JsonPreferences(path)

    assert (
        restarted.action_feedback_duration(whskr)
        is ActionFeedbackDuration.BRIEF
    )
    assert (
        restarted.action_feedback_duration(work)
        is ActionFeedbackDuration.NORMAL
    )
    saved = json.loads(path.read_text())["profiles"][str(whskr.collection.resolve())]
    assert saved["action_feedback_duration"] == "brief"


def test_failed_action_feedback_duration_write_preserves_active_and_saved_choice(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "preferences.json"
    whskr = profile(tmp_path / "Anki2")
    preferences = JsonPreferences(path)
    preferences.set_action_feedback_duration(whskr, ActionFeedbackDuration.BRIEF)

    def fail_replace(_source, _destination):
        raise OSError("disk unavailable")

    monkeypatch.setattr(Path, "replace", fail_replace)

    with pytest.raises(OSError, match="disk unavailable"):
        preferences.set_action_feedback_duration(
            whskr, ActionFeedbackDuration.RELAXED
        )

    assert (
        preferences.action_feedback_duration(whskr)
        is ActionFeedbackDuration.BRIEF
    )
    assert (
        JsonPreferences(path).action_feedback_duration(whskr)
        is ActionFeedbackDuration.BRIEF
    )


@pytest.mark.parametrize(
    "saved_controls",
    (
        {"undo": "j"},
        {"undo": "b"},
        {"unknown": "z"},
        {"undo": ["z"]},
    ),
)
def test_malformed_or_unsupported_review_controls_fall_back_safely(
    tmp_path, saved_controls
) -> None:
    path = tmp_path / "preferences.json"
    whskr = profile(tmp_path / "Anki2")
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "templates": {},
                "profiles": {
                    str(whskr.collection.resolve()): {
                        "name": "whskr",
                        "review_controls": saved_controls,
                    }
                },
            }
        )
    )

    assert JsonPreferences(path).review_controls(whskr) == ReviewControls.defaults()


def test_failed_review_control_write_preserves_active_and_saved_mapping(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "preferences.json"
    whskr = profile(tmp_path / "Anki2")
    preferences = JsonPreferences(path)
    original = ReviewControls.defaults().with_binding(ReviewAction.UNDO, "z")
    preferences.set_review_controls(whskr, original)

    def fail_replace(_source, _destination):
        raise OSError("disk unavailable")

    monkeypatch.setattr(Path, "replace", fail_replace)

    changed = original.with_binding(ReviewAction.UNDO, "v")
    with pytest.raises(OSError, match="disk unavailable"):
        preferences.set_review_controls(whskr, changed)

    assert preferences.review_controls(whskr) == original
    assert JsonPreferences(path).review_controls(whskr) == original


@pytest.mark.parametrize("choice", ["enabled", "setting"])
def test_nested_add_on_draft_is_atomic_and_retry_preserves_other_preferences(
    tmp_path, monkeypatch, choice,
) -> None:
    path = tmp_path / "preferences.json"
    whskr = profile(tmp_path / "Anki2")
    preferences = JsonPreferences(path)
    preferences.set_add_on_enabled(whskr, "celebration", enabled=True)
    preferences.set_add_on_setting(whskr, "celebration", "duration", "brief")
    preferences.set_mode(JAPANESE_RECOGNITION, "meaning", SectionMode.FOLD)
    saved = path.read_bytes()

    def change():
        if choice == "enabled":
            preferences.set_add_on_enabled(whskr, "celebration", enabled=False)
        else:
            preferences.set_add_on_setting(whskr, "celebration", "duration", "long")

    def fail_replace(_source, _destination):
        raise OSError("disk unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", fail_replace)
        with pytest.raises(OSError, match="disk unavailable"):
            change()
    assert preferences.add_on_enabled(whskr, "celebration")
    assert preferences.add_on_settings(whskr, "celebration") == {"duration": "brief"}
    assert path.read_bytes() == saved
    change()
    restarted = JsonPreferences(path)
    for reader in (preferences, restarted):
        assert reader.add_on_enabled(whskr, "celebration") is (choice != "enabled")
        assert reader.add_on_settings(whskr, "celebration") == {
            "duration": "long" if choice == "setting" else "brief"
        }
        assert reader.mode(JAPANESE_RECOGNITION, "meaning") is SectionMode.FOLD
