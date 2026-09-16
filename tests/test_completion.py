from rich.cells import cell_len

from repetui.addons import (
    AddOnEvent,
    AddOnEventType,
    ChoiceSetting,
    PresentationCue,
    PresentationCueType,
    bundled_add_ons,
)
from repetui.completion import compose_completion_frame


def test_completion_celebration_is_bundled_with_declarative_duration() -> None:
    definitions = bundled_add_ons()

    assert len(definitions) == 1
    definition = definitions[0]
    assert definition.id == "completion-celebration"
    assert definition.name == "Completion Celebration"
    assert definition.events == frozenset({AddOnEventType.REVIEW_COMPLETED})
    assert definition.settings == (
        ChoiceSetting(
            "duration",
            "Duration",
            ("short", "medium", "long"),
            "medium",
        ),
    )
    assert definition.handle(
        AddOnEvent(AddOnEventType.REVIEW_COMPLETED, deck_name="Japanese"),
        {"duration": "short"},
    ) == PresentationCue(
        PresentationCueType.COMPLETION_CELEBRATION,
        values={"deck_name": "Japanese", "duration": "short"},
    )


def test_completion_art_keeps_its_message_inside_small_panes() -> None:
    standard = compose_completion_frame(40, 6, "Languages::日本語", phase=0)
    narrow = compose_completion_frame(7, 4, "Languages::日本語", phase=3)

    assert "deck complete" in standard.plain
    assert "日本語" in standard.plain
    assert len(standard.plain.splitlines()) == 6
    assert all(cell_len(line) == 40 for line in standard.plain.splitlines())
    assert "done" in narrow.plain
    assert len(narrow.plain.splitlines()) == 4
    assert all(cell_len(line) == 7 for line in narrow.plain.splitlines())
