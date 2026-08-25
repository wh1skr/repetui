import json

from repetui.preferences import (
    SectionMode,
    SectionPreferences,
    default_preferences_path,
)
from repetui.presentation import CardTemplateIdentity

JAPANESE_RECOGNITION = CardTemplateIdentity(204, "Japanese", 0, "Recognition")
JAPANESE_PRODUCTION = CardTemplateIdentity(204, "Japanese", 1, "Production")
AWS_BASIC = CardTemplateIdentity(305, "AWS", 0, "Basic")


def test_unknown_sections_default_to_show_without_writing_a_file(tmp_path) -> None:
    path = tmp_path / "preferences.json"
    preferences = SectionPreferences(path)

    assert preferences.mode(JAPANESE_RECOGNITION, "back:heading:mnemonic") is SectionMode.SHOW
    assert not path.exists()


def test_section_modes_survive_restart_and_are_scoped_to_template(tmp_path) -> None:
    path = tmp_path / "preferences.json"
    preferences = SectionPreferences(path)
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

    restarted = SectionPreferences(path)

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
