"""Pure composition policy for the compact Flow review surface."""

from __future__ import annotations

import re
from dataclasses import dataclass

from rich.cells import cell_len
from rich.text import Text

from .backend import DueCounts
from .preferences import SectionMode
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


def _front_content(presentation: CardPresentation) -> tuple[str, bool]:
    """Compact rendered front blocks and remove a non-functional control marker."""
    raw_blocks = [block.strip() for block in _BLOCK_BREAK.split(presentation.front.text)]
    raw_blocks = [block for block in raw_blocks if block]
    has_real_content = any(
        block.replace(_TYPE_ANSWER_MARKER, "").strip() for block in raw_blocks
    )
    blocks: list[str] = []
    for raw_block in raw_blocks:
        block = raw_block.replace(_TYPE_ANSWER_MARKER, "") if has_real_content else raw_block
        block = " ".join(block.split()) if "\n" not in block else block.strip()
        if block:
            blocks.append(block)

    rows: list[str] = []
    compact: list[str] = []

    def flush_compact() -> None:
        if compact:
            rows.append(" · ".join(compact))
            compact.clear()

    for block in blocks:
        if "\n" not in block and cell_len(block) <= _SHORT_BLOCK_WIDTH:
            compact.append(block)
        else:
            flush_compact()
            rows.append(block)
    flush_compact()
    return "\n\n".join(rows), len(blocks) > 1


def _header(
    presentation: CardPresentation,
    deck_name: str,
    counts: DueCounts,
    width: int,
) -> Text:
    """Build the first Flow line, shedding metadata before card content."""
    front, multiple_front_blocks = _front_content(presentation)
    first_front, _, remaining_front = front.partition("\n")
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
        result.append(new, style="#68a8df")
        result.append("/", style="#817d76")
        result.append(learning, style="#dc6b72")
        result.append("/", style="#817d76")
        result.append(review, style="#79c98b")
        return result

    def first_row_width() -> int:
        left = first_front + (
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
        first_front = ""

    result = Text(first_front, style="bold #eee9e0", overflow="fold")
    if template := optional["template"]:
        result.append(f"  · {template}", style="#817d76")

    right = right_text()
    right_width = right.cell_len
    minimum_gap = 2 if result.plain else 0
    result.append(" " * max(minimum_gap, width - cell_len(result.plain) - right_width))
    result.append_text(right)

    if remaining_front:
        result.append("\n")
        result.append(remaining_front, style="bold #eee9e0")
    return result


def _shown_section(section: PresentationSection) -> str:
    if section.label_is_content and section.label:
        return f"{section.label} · {section.text}" if section.text else section.label
    return section.display_text


def _is_compact_section(section: PresentationSection, text: str) -> bool:
    if "\n" in text or cell_len(text) > _SHORT_BLOCK_WIDTH:
        return False
    return not (
        section.label_is_content
        and ("\n" in section.text or cell_len(section.text) > _SHORT_HEADING_BODY_WIDTH)
    )


def _expanded_body(section: PresentationSection) -> str:
    if section.label_is_content or not section.label:
        return section.text if section.label_is_content else section.display_text
    text = section.display_text
    prefix = f"{section.label}:"
    if text.casefold().startswith(prefix.casefold()):
        return text[len(prefix) :].lstrip()
    return text


def _back(states: tuple[SectionState, ...]) -> Text:
    rows: list[tuple[str, str]] = []
    compact: list[str] = []

    def flush_compact() -> None:
        if compact:
            rows.append(("  ·  ".join(compact), "#d9d5ce"))
            compact.clear()

    for state in states:
        section = state.section
        if state.mode is SectionMode.HIDE:
            continue
        if state.mode is SectionMode.SHOW:
            shown = _shown_section(section)
            if _is_compact_section(section, shown):
                compact.append(shown)
            else:
                flush_compact()
                rows.append((shown, "#d9d5ce"))
            continue

        flush_compact()
        name = section_name(section)
        if state.expanded:
            body = _expanded_body(section)
            rows.append((f"▾ {name}\n{body}", "#c6d8d0" if state.selected else "#aaa49b"))
        else:
            marker = "›" if state.selected else "▸"
            rows.append((f"{marker} {name}", "#c6d8d0" if state.selected else "#817d76"))
    flush_compact()

    result = Text(overflow="fold")
    for index, (row, style) in enumerate(rows):
        if index:
            result.append("\n")
        result.append(row, style=style)
    return result


def compose_review(
    presentation: CardPresentation,
    deck_name: str,
    counts: DueCounts,
    width: int,
    *,
    revealed: bool,
    sections: tuple[SectionState, ...] = (),
) -> Text:
    """Compose the complete visible review document without mutating state."""
    result = _header(presentation, deck_name, counts, width)
    if revealed:
        result.append("\n")
        result.append_text(_back(sections))
    return result


def compose_ratings(width: int) -> Text:
    """Keep all four Anki choices on one row at normal and tiny widths."""
    compact = width < 34
    labels = ("1A", "2H", "3G", "4E") if compact else (
        "1 again",
        "2 hard",
        "3 good",
        "4 easy",
    )
    result = Text()
    for index, (label, colour) in enumerate(
        zip(labels, ("#dc6b72", "#d7b85a", "#79c98b", "#68a8df"), strict=True)
    ):
        if index:
            result.append("  ")
        result.append(label, style=colour)
    return result
