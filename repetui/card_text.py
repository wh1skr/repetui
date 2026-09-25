"""Annotation-preserving card text, shared by interpretation and layout.

Rich Text is the sole text representation here: slicing and joining preserve
marks. Neither HTML interpretation nor review layout policy belongs here.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from urllib.parse import urlsplit

from rich.style import Style
from rich.text import Text

INLINE_FURIGANA = re.compile(
    r"(?P<base>[㐀-鿿豈-﫿々〆ヵヶ𠀀-𪛟]+)"
    r"\[(?P<reading>[ぁ-ゖァ-ヺー・]+)\]"
)


def annotated(
    text: str,
    underlines: tuple[tuple[int, int], ...] = (),
    readings: tuple[tuple[int, int, str], ...] = (),
    *, style: str = "",
) -> Text:
    # Rich strips CR/VT/FF before callers can normalize them. Canonicalize line
    # breaks first and translate raw offsets, including CRLF's removed byte.
    clean, position = _edit_map(text, [
        (match.start(), match.end(), "" if match.group() in {"\x07", "\x08"} else "\n")
        for match in re.finditer(r"\r\n|[\r\v\f\x07\x08]", text)
    ])
    result = Text(clean, style=style, overflow="fold")
    for start, end in underlines:
        if 0 <= start < end <= len(text):
            result.stylize("underline", position(start, False), position(end, True))
    for start, end, reading in readings:
        if 0 <= start < end <= len(text):
            result.stylize(
                Style(meta={"repetui_furigana": reading}),
                position(start, False), position(end, True),
            )
    return result


def annotations(text: Text) -> tuple[
    tuple[tuple[int, int], ...], tuple[tuple[int, int, str], ...]
]:
    """Export immutable marks for the presentation value objects."""
    underlines: set[tuple[int, int]] = set()
    readings: set[tuple[int, int, str]] = set()
    for span in text.spans:
        if span.start >= span.end:
            continue
        style = Style.parse(span.style) if isinstance(span.style, str) else span.style
        if style.underline:
            underlines.add((span.start, span.end))
        reading = style.meta.get("repetui_furigana")
        if isinstance(reading, str):
            readings.add((span.start, span.end, reading))
    # Slicing and joining may split one continuous underline into adjacent spans.
    merged: list[tuple[int, int]] = []
    for start, end in sorted(underlines):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return tuple(merged), tuple(sorted(readings))


def strip(text: Text) -> Text:
    plain = text.plain
    return text[len(plain) - len(plain.lstrip()):len(plain.rstrip())]


def substitute(
    text: Text, pattern: str | re.Pattern[str],
    replacement: str | Callable[[re.Match[str]], str],
) -> Text:
    """Replace text in source order, carrying overlapping marks onto replacements."""
    return _replace_ranges(text, [
        (match.start(), match.end(), replacement(match) if callable(replacement) else replacement)
        for match in re.finditer(pattern, text.plain)
    ])


def _replace_ranges(text: Text, changes: list[tuple[int, int, str]]) -> Text:
    """Translate span endpoints once, so a single reading never splits in two."""
    if not changes:
        return text.copy()
    plain, position = _edit_map(text.plain, changes)
    result = Text(plain, style=text.style, overflow="fold")
    for span in text.spans:
        start = position(span.start, False)
        end = position(span.end, True)
        if start < end:
            result.stylize(span.style, start, end)
    return result


def _edit_map(
    text: str, changes: list[tuple[int, int, str]],
) -> tuple[str, Callable[[int, bool], int]]:
    changes.sort()
    parts: list[str] = []
    cursor = 0
    for start, end, value in changes:
        assert cursor <= start <= end
        parts.extend((text[cursor:start], value))
        cursor = end
    parts.append(text[cursor:])

    def position(offset: int, end_point: bool) -> int:
        delta = 0
        for start, end, value in changes:
            if offset <= start:
                break
            if offset < end:
                return start + delta + (len(value) if end_point else 0)
            delta += len(value) - (end - start)
        return offset + delta

    return "".join(parts), position


def normalise(text: Text, av: tuple[str, ...] = ()) -> Text:
    """Normalize placeholders and whitespace without recovering marks by search."""
    text = substitute(text, "\xa0", " ")

    def sound(match: re.Match[str]) -> str:
        path = urlsplit(match.group(1).replace("\\", "/")).path
        return f"[audio: {os.path.basename(path)}]"

    text = substitute(text, re.compile(r"\[sound:([^\]]+)\]", re.I), sound)
    text = substitute(
        text, re.compile(r"\[anki:play:[^:\]]+:(\d+)\]", re.I),
        lambda match: av[int(match.group(1))] if int(match.group(1)) < len(av) else "[audio]",
    )
    text = substitute(text, re.compile(r"\[\[type:[^\]]+\]\]", re.I), "[type answer]")
    changes: list[tuple[int, int, str]] = []
    in_fence = False
    previous_blank = True
    # str.splitlines semantics, with retained offsets into the marked source.
    offset = 0
    for raw in text.plain.splitlines(keepends=True):
        content = raw.splitlines()[0] if raw.splitlines() else ""
        if content.strip() in {"```text", "```"}:
            left = len(content) - len(content.lstrip())
            in_fence = content.strip() != "```"
            compact = False
        elif in_fence:
            left = 0
            compact = False
        else:
            left = len(content) - len(content.lstrip())
            compact = True
        right = len(content.rstrip())
        if right <= left:
            changes.append((offset, offset + len(raw), "" if previous_blank else "\n"))
            previous_blank = True
        else:
            if left:
                changes.append((offset, offset + left, ""))
            if compact:
                changes.extend(
                    (offset + left + match.start(), offset + left + match.end(), " ")
                    for match in re.finditer(r"[ \t]+", content[left:right])
                )
            changes.append((
                offset + right, offset + len(raw),
                "\n" if len(raw) > len(content) else "",
            ))
            previous_blank = False
        offset += len(raw)
    return strip(_replace_ranges(text, changes))


def extract_readings(text: Text) -> Text:
    """Turn Japanese bracket readings into marks on their base characters."""
    result = text.copy()
    changes: list[tuple[int, int, str]] = []
    for match in INLINE_FURIGANA.finditer(text.plain):
        result.stylize(
            Style(meta={"repetui_furigana": match.group("reading")}),
            match.start("base"), match.end("base"),
        )
        changes.append((match.end("base"), match.end(), ""))
    return _replace_ranges(result, changes)


def inline_readings(text: Text) -> Text:
    """Insert bracketed readings before layout measures their terminal width."""
    _, readings = annotations(text)
    for end, reading in sorted({(end, reading) for _, end, reading in readings}, reverse=True):
        text = text[:end] + Text(f"[{reading}]", style="#aaa49b") + text[end:]
    return text
