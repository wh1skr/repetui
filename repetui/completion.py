"""The bundled completion-celebration add-on."""

from __future__ import annotations

from collections.abc import Mapping

from rich.cells import cell_len, split_graphemes
from rich.text import Text

from .addons import (
    AddOnDefinition,
    AddOnEvent,
    AddOnEventType,
    ChoiceSetting,
    PresentationCue,
    PresentationCueType,
    SettingValue,
)

COMPLETION_CELEBRATION_ID = "completion-celebration"
COMPLETION_DURATIONS = ("short", "medium", "long")
COMPLETION_DURATION_SECONDS = {
    "short": 0.8,
    "medium": 2.0,
    "long": 3.5,
}


def completion_duration_seconds(duration: str) -> float:
    return COMPLETION_DURATION_SECONDS.get(duration, COMPLETION_DURATION_SECONDS["medium"])


def compose_completion_frame(
    width: int,
    height: int,
    deck_name: str,
    phase: int,
) -> Text:
    """Compose restrained, terminal-native artwork for the current pane."""
    width = max(width, 1)
    height = max(height, 1)
    cells = [[(" ", "#e7e1d8") for _ in range(width)] for _ in range(height)]
    glyphs = ("·", "+", ".", ":", "*")
    colours = ("#79c98b", "#68a8df", "#d7b85a", "#dc6b72")
    for index in range(max(12, width // 2)):
        x = (index * 17 + phase * (1 + index % 3)) % width
        y = (index * 7 + phase // 2) % (height + 3) - 2
        if 0 <= y < height:
            cells[y][x] = (
                glyphs[(index + phase // 2) % len(glyphs)],
                colours[index % len(colours)],
            )

    def write_center(row: int, value: str, colour: str) -> None:
        if not 0 <= row < height:
            return
        visible = ""
        visible_width = 0
        for start, end, grapheme_width in split_graphemes(value)[0]:
            if visible_width + grapheme_width > width:
                break
            visible += value[start:end]
            visible_width += grapheme_width
        column = max((width - cell_len(visible)) // 2, 0)
        for start, end, grapheme_width in split_graphemes(visible)[0]:
            cells[row][column] = (visible[start:end], colour)
            for continuation in range(1, grapheme_width):
                cells[row][column + continuation] = ("", colour)
            column += grapheme_width

    headline = "deck complete" if width >= 13 else "complete" if width >= 8 else "done"
    middle = max(0, height // 2 - 1)
    write_center(middle, headline, "bold #eee9e0")
    if height > 1:
        write_center(middle + 1, deck_name.rsplit("::", 1)[-1], "#79c98b")

    output = Text(no_wrap=True, overflow="crop")
    for row_index, row in enumerate(cells):
        for character, style in row:
            output.append(character, style=style)
        if row_index < height - 1:
            output.append("\n")
    return output


def _handle_completion(
    event: AddOnEvent, settings: Mapping[str, SettingValue]
) -> PresentationCue:
    duration = settings.get("duration", "medium")
    return PresentationCue(
        PresentationCueType.COMPLETION_CELEBRATION,
        values={
            "deck_name": event.deck_name,
            "duration": duration if isinstance(duration, str) else "medium",
        },
    )


def completion_celebration_add_on() -> AddOnDefinition:
    """Describe the first official, disabled-by-default add-on."""
    return AddOnDefinition(
        id=COMPLETION_CELEBRATION_ID,
        name="Completion Celebration",
        description="Celebrate the final due card with a brief full-pane moment.",
        events=frozenset({AddOnEventType.REVIEW_COMPLETED}),
        settings=(
            ChoiceSetting(
                "duration",
                "Duration",
                COMPLETION_DURATIONS,
                "medium",
            ),
        ),
        handle=_handle_completion,
    )
