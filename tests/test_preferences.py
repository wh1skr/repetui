import json
from pathlib import Path

from repetui.config import ProfilePaths
from repetui.preferences import (
    JsonPreferences,
    SectionMode,
    default_preferences_path,
)
from repetui.presentation import CardTemplateIdentity

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
