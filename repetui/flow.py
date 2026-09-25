"""Pure composition policy for the compact Flow review surface."""

from __future__ import annotations

import re
from dataclasses import dataclass

from rich.cells import cell_len
from rich.text import Text

from .backend import DueCounts, ReviewQueue
from .card_text import annotated, inline_readings
from .card_text import strip as _strip_text
from .card_text import substitute as _replace_text
from .controls import ReviewAction, ReviewControls
from .preferences import AnswerLayout, SectionMode
from .presentation import CardPresentation, PresentationSection

_TYPE_ANSWER_MARKER = "[type answer]"
_BLOCK_BREAK = re.compile(r"\n\s*\n")
_SHORT_BLOCK_WIDTH = 80
_SHORT_HEADING_BODY_WIDTH = 24


@dataclass(frozen=True)
class SectionState:
    """One presentation section plus explicit user and session state."""

    section: PresentationSection
    mode: SectionMode
    expanded: bool = False
    selected: bool = False


def section_name(section: PresentationSection) -> str:
    """Return the best available human label without inventing card semantics."""
    label = section.label or section.source_label or "Answer"
    return label.replace("_", " ").strip()


def _section_text(section: PresentationSection, style: str, show_readings: bool) -> Text:
    """Attach annotations before layout transforms the section text."""
    result = annotated(section.text, section.underlines, section.furigana, style=style)
    return inline_readings(result) if show_readings else result


def _blocks(text: Text) -> list[Text]:
    """Split blank-line blocks without losing annotation offsets."""
    blocks: list[Text] = []
    start = 0
    for match in _BLOCK_BREAK.finditer(text.plain):
        blocks.append(_strip_text(text[start:match.start()]))
        start = match.end()
    blocks.append(_strip_text(text[start:]))
    return [block for block in blocks if block]


def _front_content(presentation: CardPresentation, show_readings: bool) -> tuple[Text, bool]:
    """Compact rendered front blocks and remove a non-functional control marker."""
    source = Text("\n\n").join(
        _shown_section(section, "bold #eee9e0", show_readings, separator="\n")
        for section in presentation.front.sections
    )
    raw_blocks = _blocks(source)
    has_real_content = any(
        block.plain.replace(_TYPE_ANSWER_MARKER, "").strip() for block in raw_blocks
    )
    blocks: list[Text] = []
    for raw_block in raw_blocks:
        block = (
            _replace_text(raw_block, re.escape(_TYPE_ANSWER_MARKER), "")
            if has_real_content else raw_block
        )
        block = _strip_text(block)
        if "\n" not in block.plain:
            block = _replace_text(block, r"\s+", " ")
        if block:
            blocks.append(block)

    rows: list[Text] = []
    compact: list[Text] = []

    def flush_compact() -> None:
        if compact:
            rows.append(Text(" · ", style="bold #eee9e0").join(compact))
            compact.clear()

    for block in blocks:
        if "\n" not in block.plain and block.cell_len <= _SHORT_BLOCK_WIDTH:
            compact.append(block)
        else:
            flush_compact()
            rows.append(block)
    flush_compact()
    return Text("\n\n").join(rows), len(blocks) > 1


def _header(
    presentation: CardPresentation,
    deck_name: str,
    counts: DueCounts,
    width: int,
    current_queue: ReviewQueue | None,
    show_readings: bool,
) -> Text:
    """Build the first Flow line, shedding metadata before card content."""
    front, multiple_front_blocks = _front_content(presentation, show_readings)
    first_break = front.plain.find("\n")
    first_front = front if first_break < 0 else front[:first_break]
    remaining_front = Text() if first_break < 0 else front[first_break + 1:]
    split = f"{counts.new}/{counts.learning}/{counts.review}"
    optional = {
        "deck": deck_name,
        "template": "" if multiple_front_blocks else presentation.identity.template_name,
        "total": str(counts.total),
    }

    def right_text() -> Text:
        result = Text()
        if visible_deck := optional["deck"]:
            result.append(visible_deck, style="#817d76")
            result.append("  ")
        if total := optional["total"]:
            result.append(total, style="#aaa49b")
            result.append(" ")
        new, learning, review = split.split("/")
        result.append(
            new,
            style=(
                "#68a8df underline"
                if current_queue is ReviewQueue.NEW
                else "#68a8df"
            ),
        )
        result.append("/", style="#817d76")
        result.append(
            learning,
            style=(
                "#dc6b72 underline"
                if current_queue is ReviewQueue.LEARNING
                else "#dc6b72"
            ),
        )
        result.append("/", style="#817d76")
        result.append(
            review,
            style=(
                "#79c98b underline"
                if current_queue is ReviewQueue.REVIEW
                else "#79c98b"
            ),
        )
        return result

    def first_row_width() -> int:
        left = first_front.plain + (
            f"  · {optional['template']}" if optional["template"] else ""
        )
        right = right_text()
        return cell_len(left) + (2 + right.cell_len if left else right.cell_len)

    width = max(width, 1)
    for name in ("deck", "template", "total"):
        if first_row_width() <= width:
            break
        optional[name] = ""

    if first_row_width() > width:
        remaining_front = front
        first_front = Text()

    result = first_front.copy()
    result.overflow = "fold"
    if template := optional["template"]:
        result.append(f"  · {template}", style="#817d76")

    right = right_text()
    right_width = right.cell_len
    minimum_gap = 2 if result.plain else 0
    result.append(" " * max(minimum_gap, width - cell_len(result.plain) - right_width))
    result.append_text(right)

    if remaining_front:
        result.append("\n")
        result.append_text(remaining_front)
    return result


def _shown_section(
    section: PresentationSection, style: str, show_readings: bool, *, separator: str = " · "
) -> Text:
    body = _section_text(section, style, show_readings)
    if section.label_is_content and section.label:
        result = Text(section.label, style=style)
        if body:
            result.append(separator)
            result.append_text(body)
        return result
    return body


def _is_compact_section(section: PresentationSection, text: Text) -> bool:
    if "\n" in text.plain or text.cell_len > _SHORT_BLOCK_WIDTH:
        return False
    return not (
        section.label_is_content
        and ("\n" in section.text or cell_len(section.text) > _SHORT_HEADING_BODY_WIDTH)
    )


def _expanded_body(section: PresentationSection, style: str, show_readings: bool) -> Text:
    text = _section_text(section, style, show_readings)
    if section.label_is_content or not section.label:
        return text
    prefix = f"{section.label}:"
    if text.plain.casefold().startswith(prefix.casefold()):
        return _strip_text(text[len(prefix):])
    return text


def _is_unlabelled_section(section: PresentationSection) -> bool:
    return section.id.endswith(":fallback")


def _stacked_section(section: PresentationSection, show_readings: bool) -> Text:
    result = _expanded_body(section, "#d9d5ce", show_readings)
    if _is_unlabelled_section(section):
        return result
    result.append("  · ", style="#817d76")
    result.append(section_name(section), style="#817d76")
    return result


def _short_unlabelled_blocks(section: PresentationSection, text: Text) -> tuple[Text, ...]:
    if not _is_unlabelled_section(section):
        return ()
    blocks = tuple(_blocks(text))
    if len(blocks) < 2 or any(
        not block or "\n" in block.plain or block.cell_len > _SHORT_BLOCK_WIDTH
        for block in blocks
    ):
        return ()
    return blocks


def _back(
    states: tuple[SectionState, ...],
    answer_layout: AnswerLayout,
    show_readings: bool,
) -> Text:
    rows: list[Text] = []
    compact: list[Text] = []

    def flush_compact() -> None:
        if compact:
            row = Text(overflow="fold")
            for index, part in enumerate(compact):
                if index:
                    row.append("  ·  ", style="#d9d5ce")
                row.append_text(part)
            rows.append(row)
            compact.clear()

    for state in states:
        section = state.section
        if state.mode is SectionMode.HIDE:
            continue
        if state.mode is SectionMode.SHOW:
            shown = _shown_section(section, "#d9d5ce", show_readings)
            short_blocks = _short_unlabelled_blocks(section, shown)
            if short_blocks:
                flush_compact()
                rows.extend(short_blocks)
                continue
            if (
                answer_layout is AnswerLayout.STACKED
                and _is_compact_section(section, shown)
                and _expanded_body(section, "#d9d5ce", show_readings)
            ):
                flush_compact()
                rows.append(_stacked_section(section, show_readings))
            elif _is_compact_section(section, shown):
                compact.append(shown)
            else:
                flush_compact()
                rows.append(shown)
            continue

        flush_compact()
        name = section_name(section)
        if state.expanded:
            style = "#c6d8d0" if state.selected else "#aaa49b"
            row = Text(f"▾ {name}\n", style=style, overflow="fold")
            row.append_text(_expanded_body(section, style, show_readings))
            rows.append(row)
        else:
            marker = "›" if state.selected else "▸"
            rows.append(
                Text(
                    f"{marker} {name}",
                    style="#c6d8d0" if state.selected else "#817d76",
                )
            )
    flush_compact()

    result = Text(overflow="fold")
    for index, row in enumerate(rows):
        if index:
            result.append("\n")
        result.append_text(row)
    return result


def compose_review(
    presentation: CardPresentation,
    deck_name: str,
    counts: DueCounts,
    width: int,
    *,
    revealed: bool,
    sections: tuple[SectionState, ...] = (),
    current_queue: ReviewQueue | None = None,
    answer_layout: AnswerLayout = AnswerLayout.COMPACT,
    show_readings: bool = False,
) -> Text:
    """Compose the complete visible review document without mutating state."""
    result = _header(presentation, deck_name, counts, width, current_queue, show_readings)
    if revealed:
        result.append("\n")
        result.append_text(_back(sections, answer_layout, show_readings))
    return result


def compose_ratings(width: int, controls: ReviewControls | None = None) -> Text:
    """Keep all four Anki choices on one row at normal and tiny widths."""
    controls = controls or ReviewControls.defaults()
    choices = (
        (ReviewAction.AGAIN, "again", "A", "#dc6b72"),
        (ReviewAction.HARD, "hard", "H", "#d7b85a"),
        (ReviewAction.GOOD, "good", "G", "#79c98b"),
        (ReviewAction.EASY, "easy", "E", "#68a8df"),
    )
    keys = [
        controls.binding(action) or "-"
        for action, _label, _short, _colour in choices
    ]
    full_labels = [
        f"{key} {label}"
        for key, (_action, label, _short, _colour) in zip(
            keys, choices, strict=True
        )
    ]
    compact = sum(cell_len(label) for label in full_labels) + 6 > width
    labels = (
        [
            f"{key[:3]}{short}"
            for key, (_action, _label, short, _colour) in zip(
                keys, choices, strict=True
            )
        ]
        if compact
        else full_labels
    )
    result = Text()
    for index, (label, colour) in enumerate(
        zip(labels, (choice[3] for choice in choices), strict=True)
    ):
        if index:
            result.append("  ")
        result.append(label, style=colour)
    return result


def compose_rating_feedback(rating: int) -> Text:
    """Identify the rating Anki accepted without echoing its triggering key."""
    choices = {
        1: ("again", "#dc6b72"),
        2: ("hard", "#d7b85a"),
        3: ("good", "#79c98b"),
        4: ("easy", "#68a8df"),
    }
    try:
        label, colour = choices[rating]
    except KeyError as exc:
        raise ValueError("Rating must be between 1 and 4.") from exc

    result = Text("rated · ", style="#817d76", no_wrap=True)
    result.append(f"{rating} {label}", style=f"bold {colour}")
    return result
