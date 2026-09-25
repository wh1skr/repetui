"""Lossless terminal-native presentation of rendered Anki cards.

The module is deliberately pure: callers provide rendered card content and receive
immutable sections. Ordinary cards display their rendered sides. When dynamic markup
cannot be separated confidently, an inferred profile may use only source fields whose
content is demonstrably present on that rendered side; users can then confirm or replace
the field mapping explicitly.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from dataclasses import field as dataclass_field
from html.parser import HTMLParser
from typing import Literal
from urllib.parse import urlsplit

from rich.cells import cell_len
from rich.text import Text

from .card_text import INLINE_FURIGANA, annotated, annotations, extract_readings, normalise

_ANSWER_RULE = re.compile(r'<hr[^>]*\bid\s*=\s*["\']?answer["\']?[^>]*>', re.IGNORECASE)
_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_CSS_RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")
_SIMPLE_CLASS = re.compile(r"(?:[a-z][\w-]*)?\.([\w-]+)", re.IGNORECASE)
_FURIGANA_HINT = re.compile(r"(?:\[[ぁ-んァ-ンー]{1,30}\]|（[ぁ-んァ-ンー]{1,30}）)")
_MACHINE_VALUE = re.compile(
    r"(?:https?://\S+|[0-9]{3,}|[0-9a-f]{12,}(?::[0-9]+)?)",
    re.IGNORECASE,
)
_UNDERLINED_HEADING = re.compile(
    r"<u\b[^>]*>\s*(?:<span\b[^>]*>\s*)?"
    r"<(?P<tag>b|strong)\b[^>]*>(?P<label>[^<>]{1,80})</(?P=tag)>"
    r"(?:\s*</span>)?\s*</u>",
    re.IGNORECASE,
)
_INLINE_LABEL = re.compile(
    r"<(?P<tag>b|strong)\b[^>]*>\s*(?P<label>[^<>:\r\n]{1,80}):\s*</(?P=tag)>",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CardTemplateIdentity:
    """Stable identity for one Anki card template."""

    note_type_id: int
    note_type_name: str
    template_ordinal: int
    template_name: str


@dataclass(frozen=True)
class SourceField:
    """A note field available for section identification or profile display."""

    name: str
    html: str


@dataclass(frozen=True)
class TemplateFieldProfile:
    """Ordered source fields used for a terminal-native card presentation."""

    prompt_fields: tuple[str, ...]
    answer_fields: tuple[str, ...]
    ignored_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class FieldLayoutSuggestion:
    """A conservative multi-card proposal and the evidence behind it."""

    profile: TemplateFieldProfile | None
    confidence: Literal["high", "low"]
    reasons: tuple[str, ...]
    unresolved_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class AVReference:
    """Meaningful fallback for one indexed rendered Anki AV marker."""

    kind: str
    label: str | None = None

    @property
    def placeholder(self) -> str:
        return f"[{self.kind}: {self.label}]" if self.label else f"[{self.kind}]"


@dataclass(frozen=True)
class RawCardContent:
    """Rendered Anki payload accepted at the presentation seam."""

    identity: CardTemplateIdentity
    front_html: str
    back_html: str
    fields: tuple[SourceField, ...] = ()
    front_av: tuple[AVReference, ...] = ()
    back_av: tuple[AVReference, ...] = ()
    card_css: str = ""


@dataclass(frozen=True)
class PresentationSection:
    """One ordered section whose content came from the rendered card side."""

    id: str
    text: str
    label: str | None = None
    source_label: str | None = None
    label_is_content: bool = False
    underlines: tuple[tuple[int, int], ...] = ()
    furigana: tuple[tuple[int, int, str], ...] = ()

    @property
    def display_text(self) -> str:
        if not self.label_is_content:
            return self.text
        if self.label and self.text:
            return f"{self.label}\n{self.text}"
        return self.label or self.text


@dataclass(frozen=True)
class CardSide:
    """All visible content for one side in display order."""

    sections: tuple[PresentationSection, ...]

    @property
    def text(self) -> str:
        return "\n\n".join(section.display_text for section in self.sections)

    @property
    def display_width(self) -> int:
        return max((_terminal_width(line) for line in self.text.splitlines()), default=0)


@dataclass(frozen=True)
class CardPresentation:
    identity: CardTemplateIdentity
    front: CardSide
    back: CardSide
    suggested_profile: TemplateFieldProfile | None = None


@dataclass
class _RawSection:
    kind: str
    start: int
    label_end: int
    end: int


@dataclass
class _RubyCapture:
    base_start: int
    rt_depth: int = 0
    rp_depth: int = 0
    rt_base_end: int = 0
    reading_parts: list[str] = dataclass_field(default_factory=list)


class _RenderedHTMLParser(HTMLParser):
    """Render readable text while retaining safe structural section hints."""

    _BLOCKS = {
        "address",
        "article",
        "aside",
        "blockquote",
        "div",
        "dl",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "header",
        "main",
        "nav",
        "ol",
        "p",
        "section",
        "table",
        "ul",
    }
    _HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
    _SUPPRESSED = {"script", "style", "template"}
    _VOID = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "source",
        "wbr",
    }

    def __init__(self, underline_classes: frozenset[str] = frozenset()) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.sections: list[_RawSection] = []
        self._active: _RawSection | None = None
        self._heading_depth = 0
        self._ignored_depth = 0
        self._pre_depth = 0
        self._math_tag: str | None = None
        self._math_nested_depth = 0
        self._media_tag: str | None = None
        self._media_emitted = False
        self._row_cells = 0
        self._underline_classes = underline_classes
        self._underline_depth = 0
        self._underline_stack: list[tuple[str, bool]] = []
        self._raw_length = 0
        self.underline_ranges: list[tuple[int, int]] = []
        self._ruby_stack: list[_RubyCapture] = []
        self.ruby_ranges: list[tuple[int, int, str]] = []

    def _emit(self, text: str) -> None:
        start = self._raw_length
        self._raw_length += len(text)
        if self._underline_depth and text:
            if self.underline_ranges and self.underline_ranges[-1][1] == start:
                previous, _ = self.underline_ranges[-1]
                self.underline_ranges[-1] = (previous, self._raw_length)
            else:
                self.underline_ranges.append((start, self._raw_length))
        self.parts.append(text)
        if self._active is not None:
            self._active.end = self._raw_length
            if self._heading_depth:
                self._active.label_end = self._raw_length

    @staticmethod
    def _classes(attributes: dict[str, str | None]) -> set[str]:
        return set((attributes.get("class") or "").lower().split())

    @staticmethod
    def _is_hidden(attributes: dict[str, str | None]) -> bool:
        style = (attributes.get("style") or "").casefold().replace(" ", "")
        classes = set((attributes.get("class") or "").casefold().split())
        return (
            "hidden" in attributes
            or "hidden" in classes
            or attributes.get("aria-hidden", "").lower() == "true"
            or "display:none" in style
            or "visibility:hidden" in style
        )

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attributes = dict(attrs)
        if tag not in self._VOID:
            active = (
                not self._ignored_depth
                and self._math_tag is None
                and tag not in self._SUPPRESSED
                and not self._is_hidden(attributes)
            )
            underlined = active and (
                tag in {"u", "ins"}
                or _declares_underline(attributes.get("style") or "") is True
                or bool(self._classes(attributes) & self._underline_classes)
            )
            self._underline_stack.append((tag, underlined))
            self._underline_depth += int(underlined)
        if self._ignored_depth:
            if tag not in self._VOID:
                self._ignored_depth += 1
            return
        if tag in self._SUPPRESSED or self._is_hidden(attributes):
            if tag not in self._VOID:
                self._ignored_depth = 1
            return
        if self._math_tag is not None:
            if tag not in self._VOID:
                self._math_nested_depth += 1
            return

        ruby = self._ruby_stack[-1] if self._ruby_stack else None
        if ruby is not None and ruby.rt_depth:
            if tag not in self._VOID:
                ruby.rt_depth += 1
            return
        if ruby is not None and ruby.rp_depth:
            if tag not in self._VOID:
                ruby.rp_depth += 1
            return
        if tag == "ruby":
            self._ruby_stack.append(_RubyCapture(self._raw_length))
            return
        if ruby is not None and tag == "rt":
            ruby.rt_base_end = self._raw_length
            ruby.rt_depth = 1
            ruby.reading_parts.clear()
            return
        if ruby is not None and tag == "rp":
            ruby.rp_depth = 1
            return

        classes = self._classes(attributes)
        if tag == "svg":
            self._emit("[math]" if "latex" in classes else "[image]")
            self._ignored_depth = 1
        elif tag in self._HEADINGS:
            self._emit("\n")
            self._active = _RawSection(
                "heading", self._raw_length, self._raw_length, self._raw_length
            )
            self.sections.append(self._active)
            self._heading_depth = 1
        elif tag == "repetui-label":
            self._active = _RawSection(
                "label", self._raw_length, self._raw_length, self._raw_length
            )
            self.sections.append(self._active)
            self._heading_depth = 1
        elif tag == "br":
            self._emit("\n")
        elif tag == "hr":
            self._emit("\n────────\n")
        elif tag == "li":
            self._emit("\n• ")
        elif tag == "tr":
            self._row_cells = 0
            self._emit("\n")
        elif tag in {"td", "th"}:
            if self._row_cells:
                self._emit(" │ ")
            self._row_cells += 1
        elif tag == "rt":
            self._emit("（")
        elif tag == "pre":
            self._pre_depth += 1
            self._emit("\n```text\n")
        elif tag == "code" and not self._pre_depth:
            self._emit("`")
        elif tag in {"math", "anki-mathjax"} or "mathjax" in classes:
            self._math_tag = tag
            self._math_nested_depth = 0
            self._emit("[math: ")
        elif tag == "img":
            label = attributes.get("alt") or attributes.get("title")
            if not label:
                label = _media_name(attributes.get("src") or "")
            kind = "math" if "latex" in classes else "image"
            self._emit(f"[{kind}: {label.strip()}]" if label else f"[{kind}]")
        elif tag in {"audio", "video"}:
            self._media_tag = tag
            label = attributes.get("title") or _media_name(attributes.get("src") or "")
            self._media_emitted = bool(label)
            if label:
                self._emit(f"[{tag}: {label}]")
        elif tag == "source" and self._media_tag and not self._media_emitted:
            label = _media_name(attributes.get("src") or "")
            if label:
                self._emit(f"[{self._media_tag}: {label}]")
                self._media_emitted = True
        elif tag == "input" and (
            attributes.get("id", "").lower() == "typeans" or "typeans" in classes
        ):
            self._emit("[type answer]")
        elif tag in self._BLOCKS:
            self._emit("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for index in range(len(self._underline_stack) - 1, -1, -1):
            if self._underline_stack[index][0] == tag:
                for _, underlined in self._underline_stack[index:]:
                    self._underline_depth -= int(underlined)
                del self._underline_stack[index:]
                break
        if self._ignored_depth:
            self._ignored_depth -= 1
            return
        if self._math_tag is not None:
            if self._math_nested_depth:
                self._math_nested_depth -= 1
            elif tag == self._math_tag:
                self._emit("]")
                self._math_tag = None
            return
        ruby = self._ruby_stack[-1] if self._ruby_stack else None
        if ruby is not None and ruby.rt_depth:
            ruby.rt_depth -= 1
            if not ruby.rt_depth:
                reading = _normalise("".join(ruby.reading_parts))
                if reading and ruby.base_start < ruby.rt_base_end:
                    self.ruby_ranges.append(
                        (ruby.base_start, ruby.rt_base_end, reading)
                    )
                elif reading:
                    self._emit(f"（{reading}）")
                ruby.base_start = self._raw_length
            return
        if ruby is not None and ruby.rp_depth:
            ruby.rp_depth -= 1
            return
        if ruby is not None and tag == "ruby":
            self._ruby_stack.pop()
            return
        if tag in self._HEADINGS and self._heading_depth:
            self._heading_depth = 0
            self._emit("\n")
        elif tag == "repetui-label" and self._heading_depth:
            self._heading_depth = 0
        elif tag == "rt":
            self._emit("）")
        elif tag == "pre" and self._pre_depth:
            self._emit("\n```\n")
            self._pre_depth -= 1
        elif tag == "code" and not self._pre_depth:
            self._emit("`")
        elif tag in {"audio", "video"}:
            self._media_tag = None
            self._media_emitted = False
        elif tag in self._BLOCKS or tag == "tr":
            self._emit("\n")

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        ruby = self._ruby_stack[-1] if self._ruby_stack else None
        if ruby is not None and ruby.rt_depth:
            ruby.reading_parts.append(data)
        elif ruby is None or not ruby.rp_depth:
            self._emit(data)


class _ClassCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.classes: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.classes.update((dict(attrs).get("class") or "").casefold().split())


def _html_classes(html: str) -> frozenset[str]:
    parser = _ClassCollector()
    parser.feed(html)
    return frozenset(parser.classes)


@dataclass(frozen=True)
class _RenderedDocument:
    text: str
    structural_sections: tuple[tuple[str, Text, Text], ...]
    prelude: Text
    atoms: tuple[str, ...]
    underlines: tuple[tuple[int, int], ...] = ()
    furigana: tuple[tuple[int, int, str], ...] = ()


def _declares_underline(declarations: str) -> bool | None:
    result: bool | None = None
    for declaration in declarations.split(";"):
        name, separator, value = declaration.partition(":")
        if separator and name.strip().casefold() in {
            "text-decoration",
            "text-decoration-line",
        }:
            result = bool(re.search(r"\bunderline\b", value, re.IGNORECASE))
    return result


def _underlined_classes(css: str) -> frozenset[str]:
    """Recognize only simple class selectors with explicit underline rules."""
    result: set[str] = set()
    for selectors, declarations in _CSS_RULE.findall(_CSS_COMMENT.sub("", css)):
        underlined = _declares_underline(declarations)
        if underlined is None:
            continue
        for selector in selectors.split(","):
            match = _SIMPLE_CLASS.fullmatch(selector.strip())
            if match:
                name = match.group(1).casefold()
                if underlined:
                    result.add(name)
                else:
                    result.discard(name)
    return frozenset(result)


def _without_inline_furigana(text: str) -> str:
    return INLINE_FURIGANA.sub(lambda match: match.group("base"), text)


def _promote_structural_labels(html: str) -> str:
    html = _UNDERLINED_HEADING.sub(lambda match: f"<h6>{match.group('label')}</h6>", html)
    return _INLINE_LABEL.sub(
        lambda match: f"<repetui-label>{match.group('label')}:</repetui-label>", html
    )


def _render_document(
    html: str,
    av: tuple[AVReference, ...] = (),
    *,
    card_css: str = "",
) -> _RenderedDocument:
    parser = _RenderedHTMLParser(_underlined_classes(card_css))
    parser.feed(_promote_structural_labels(html))
    parser.close()
    raw_text = "".join(parser.parts)
    placeholders = tuple(reference.placeholder for reference in av)

    def render_range(start: int, end: int) -> Text:
        source = annotated(
            raw_text[start:end],
            tuple(
                (max(left, start) - start, min(right, end) - start)
                for left, right in parser.underline_ranges if left < end and right > start
            ),
            tuple(
                (max(left, start) - start, min(right, end) - start, reading)
                for left, right, reading in parser.ruby_ranges if left < end and right > start
            ),
        )
        return extract_readings(normalise(source, placeholders))

    marked = render_range(0, len(raw_text))
    text = marked.plain
    underlines, furigana = annotations(marked)
    sections = tuple(
        (
            section.kind,
            render_range(section.start, section.label_end),
            render_range(section.label_end, section.end),
        )
        for section in parser.sections
    )
    atoms = tuple(
        atom
        for part in parser.parts
        if (atom := _without_inline_furigana(_normalise(part, av)))
    )
    return _RenderedDocument(
        text,
        sections,
        render_range(0, parser.sections[0].start if parser.sections else len(raw_text)),
        atoms,
        underlines,
        furigana,
    )


def _normalise(text: str, av: tuple[AVReference, ...] = ()) -> str:
    return normalise(annotated(text), tuple(reference.placeholder for reference in av)).plain


def _reconcile_key(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _slug(value: str) -> str:
    slug = "".join(character if character.isalnum() else "-" for character in value.casefold())
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug or "section"


def _label_key(value: str) -> str:
    words = "".join(character if character.isalnum() else " " for character in value.casefold())
    return " ".join(words.split())


def _media_name(source: str) -> str:
    path = urlsplit(source.replace("\\", "/")).path
    return os.path.basename(path)


def _unique_id(prefix: str, value: str, seen: dict[str, int]) -> str:
    base = f"{prefix}:{_slug(value)}"
    seen[base] = seen.get(base, 0) + 1
    return base if seen[base] == 1 else f"{base}:{seen[base]}"


def _with_document_marks(
    section: PresentationSection,
    document: _RenderedDocument,
    offset: int,
) -> PresentationSection:
    if not section.text:
        return section
    if document.text[offset : offset + len(section.text)] != section.text:
        return section
    end = offset + len(section.text)
    underlines, furigana = annotations(
        annotated(document.text, document.underlines, document.furigana)[offset:end]
    )
    return replace(section, underlines=underlines, furigana=furigana)


def _structural_sections(side: str, document: _RenderedDocument) -> tuple[PresentationSection, ...]:
    if not document.structural_sections:
        return ()
    seen: dict[str, int] = {}
    result: list[PresentationSection] = []
    if document.prelude:
        underlines, furigana = annotations(document.prelude)
        result.append(PresentationSection(
            f"{side}:preamble", document.prelude.plain,
            underlines=underlines, furigana=furigana,
        ))
    for kind, raw_label, body in document.structural_sections:
        label = raw_label.plain.removesuffix(":").strip()
        if label or body:
            if kind == "label" or raw_label.plain.rstrip().endswith(":"):
                text = normalise(Text(" ").join((raw_label, body)))
                underlines, furigana = annotations(text)
                result.append(
                    PresentationSection(
                        _unique_id(f"{side}:label", label, seen),
                        text.plain,
                        label=label or None,
                        underlines=underlines,
                        furigana=furigana,
                    )
                )
            else:
                underlines, furigana = annotations(body)
                result.append(
                    PresentationSection(
                        _unique_id(f"{side}:heading", label, seen),
                        body.plain,
                        label=label or None,
                        label_is_content=True,
                        underlines=underlines,
                        furigana=furigana,
                    )
                )
    joined = "\n\n".join(section.display_text for section in result)
    if _reconcile_key(joined) != _reconcile_key(document.text):
        return ()
    return tuple(result)


def _field_sections(
    side: str, document: _RenderedDocument, fields: tuple[SourceField, ...]
) -> tuple[PresentationSection, ...]:
    matches: list[tuple[int, int, str]] = []
    for field in fields:
        field_text = _render_document(field.html).text
        if not field_text:
            continue
        start = document.text.find(field_text)
        if start >= 0 and document.text.find(field_text, start + 1) < 0:
            matches.append((start, start + len(field_text), field.name))
    matches.sort()
    if len(matches) < 2:
        return ()
    if any(
        current[0] < previous[1] for previous, current in zip(matches, matches[1:], strict=False)
    ):
        return ()
    cursor = 0
    for start, end, _ in matches:
        if document.text[cursor:start].strip():
            return ()
        cursor = end
    if document.text[cursor:].strip():
        return ()

    seen: dict[str, int] = {}
    sections = tuple(
        PresentationSection(
            _unique_id(f"{side}:field", name, seen),
            text,
            source_label=name,
        )
        for start, end, name in matches
        for text in (document.text[start:end],)
    )
    joined = "\n\n".join(section.display_text for section in sections)
    return (
        tuple(
            _with_document_marks(section, document, start)
            for section, (start, _end, _name) in zip(sections, matches, strict=True)
        )
        if _reconcile_key(joined) == _reconcile_key(document.text)
        else ()
    )


def _with_source_labels(
    sections: tuple[PresentationSection, ...], fields: tuple[SourceField, ...]
) -> tuple[PresentationSection, ...]:
    rendered_fields = [(field.name, _render_document(field.html).text) for field in fields]
    result: list[PresentationSection] = []
    for section in sections:
        source_label = next(
            (
                name
                for name, text in rendered_fields
                if text
                and (
                    _reconcile_key(text) == _reconcile_key(section.text)
                    or (section.label and _label_key(name) == _label_key(section.label))
                )
            ),
            None,
        )
        result.append(replace(section, source_label=source_label))
    return tuple(result)


def _present_side(
    side: str,
    html: str,
    fields: tuple[SourceField, ...],
    av: tuple[AVReference, ...],
) -> CardSide:
    document = _render_document(html, av)
    if not document.text:
        label = "Question" if side == "front" else "Answer"
        return CardSide((PresentationSection(f"{side}:fallback", "(empty card)", label),))

    sections = _structural_sections(side, document)
    if sections:
        return CardSide(_with_source_labels(sections, fields))
    sections = _field_sections(side, document, fields)
    if sections:
        return CardSide(sections)

    label = "Question" if side == "front" else "Answer"
    return CardSide(
        (
            PresentationSection(
                f"{side}:fallback",
                document.text,
                label,
                underlines=document.underlines,
                furigana=document.furigana,
            ),
        )
    )


def _strip_answer_html(back_html: str) -> tuple[str, bool]:
    match = _ANSWER_RULE.search(back_html)
    if match:
        return back_html[match.end() :], True
    return back_html, False


def _strip_plain_front(back: CardSide, front: CardSide) -> CardSide:
    front_text = front.text
    back_text = back.text
    if len(back.sections) > 1 and back.sections[0].display_text == front_text:
        return CardSide(back.sections[1:])
    if back_text.startswith(front_text) and (
        len(back_text) == len(front_text)
        or back_text[len(front_text)].isspace()
        or back_text[len(front_text)] == "─"
    ):
        remainder = back_text[len(front_text) :].lstrip("\n ─")
        if remainder:
            if len(back.sections) == 1:
                section = back.sections[0]
                offset = len(section.text) - len(remainder)
                if offset >= 0 and section.text[offset:] == remainder:
                    end = offset + len(remainder)
                    underlines, furigana = annotations(
                        annotated(section.text, section.underlines, section.furigana)[offset:end]
                    )
                    return CardSide(
                        (
                            replace(
                                section,
                                id="back:fallback",
                                text=remainder,
                                label="Answer",
                                underlines=underlines,
                                furigana=furigana,
                            ),
                        )
                    )
            return CardSide((PresentationSection("back:fallback", remainder, "Answer"),))
    return back


def _terminal_width(text: str) -> int:
    return cell_len(text)


def _field_is_present(field: _RenderedDocument, side: _RenderedDocument) -> bool:
    key = _reconcile_key(field.text)
    if len(key) <= 2 and key.isascii() and key.isalnum():
        return any(_reconcile_key(atom) == key for atom in side.atoms)
    return key in _reconcile_key(side.text)


def _suggestion_key(text: str) -> str:
    """Compare field and rendered-side text despite common ruby/furigana forms."""
    without_readings = _FURIGANA_HINT.sub("", text)
    return re.sub(r"\s+", "", without_readings).casefold()


def _suggestion_position(field: _RenderedDocument, side: _RenderedDocument) -> int:
    key = _suggestion_key(field.text)
    if not key:
        return -1
    if (
        len(key) <= 2
        and key.isascii()
        and key.isalnum()
        and not any(_suggestion_key(atom) == key for atom in side.atoms)
    ):
        return -1
    return _suggestion_key(side.text).find(key)


def _metadata_hint(name: str) -> bool:
    return name.casefold() in {
        "key",
        "kind",
        "after",
        "id",
        "pointid",
        "releaseday",
        "releasedate",
        "sourceurl",
        "url",
        "timestamp",
    } or name.endswith(("ID", "Id", "URL", "Url", "Timestamp"))


def _metadata_evidence(name: str, values: Sequence[str]) -> bool:
    populated = [value for value in values if value]
    return _metadata_hint(name) and bool(populated) and (
        len(set(populated)) == 1
        or all(_MACHINE_VALUE.fullmatch(value) for value in populated)
    )


def suggest_field_layout(samples: Sequence[RawCardContent]) -> FieldLayoutSuggestion:
    """Propose only fields supported by repeated rendered-side evidence.

    Every sample must belong to one note type/template and expose the same field
    names. Unresolved fields remain Auto rather than being silently discarded.
    """
    if not samples:
        return FieldLayoutSuggestion(None, "low", ("More cards are needed to compare.",))
    first = samples[0]
    identity = (first.identity.note_type_id, first.identity.template_ordinal)
    names = tuple(field.name for field in first.fields)
    if any(
        (sample.identity.note_type_id, sample.identity.template_ordinal) != identity
        or tuple(field.name for field in sample.fields) != names
        for sample in samples
    ):
        raise ValueError("Samples must use the same template and source fields.")
    if len(samples) < 2:
        return FieldLayoutSuggestion(
            None, "low", ("More cards are needed for a reliable layout suggestion.",), names
        )
    if len({tuple(field.html for field in sample.fields) for sample in samples}) < 2:
        return FieldLayoutSuggestion(
            None,
            "low",
            ("More varied cards are needed for a reliable layout suggestion.",),
            names,
        )

    values: dict[str, list[str]] = {name: [] for name in names}
    front_positions: dict[str, list[int]] = {name: [] for name in names}
    back_positions: dict[str, list[int]] = {name: [] for name in names}
    back_only_markup_hits: dict[str, int] = {name: 0 for name in names}
    for sample in samples:
        front = _render_document(sample.front_html, sample.front_av)
        back = _render_document(sample.back_html, sample.back_av)
        front_classes = _html_classes(sample.front_html)
        back_classes = _html_classes(sample.back_html)
        for field in sample.fields:
            rendered = _render_document(field.html)
            values[field.name].append(_suggestion_key(rendered.text))
            if not rendered.text:
                continue
            classes = _html_classes(field.html)
            if classes and classes <= back_classes and classes.isdisjoint(front_classes):
                back_only_markup_hits[field.name] += 1
            front_position = _suggestion_position(rendered, front)
            back_position = _suggestion_position(rendered, back)
            if front_position >= 0:
                front_positions[field.name].append(front_position)
            if back_position >= 0:
                back_positions[field.name].append(back_position)

    duplicates: set[str] = set()
    ambiguous_duplicates: set[str] = set()
    split_prompt: set[str] = set()
    split_answer: set[str] = set()
    by_values: dict[tuple[str, ...], list[str]] = {}
    for name in names:
        if any(values[name]):
            by_values.setdefault(tuple(values[name]), []).append(name)
    for group in by_values.values():
        if len(group) < 2:
            continue
        if len(group) == 2:
            back_styled = [
                name
                for name in group
                if back_only_markup_hits[name] == sum(bool(value) for value in values[name])
                and back_only_markup_hits[name] >= 2
            ]
            if len(back_styled) == 1:
                back_name = back_styled[0]
                front_name = next(name for name in group if name != back_name)
                if not _metadata_hint(front_name) and len(front_positions[front_name]) >= 2:
                    split_prompt.add(front_name)
                    split_answer.add(back_name)
                    continue
        if len(
            {
                tuple(sample.fields[names.index(name)].html for sample in samples)
                for name in group
            }
        ) > 1:
            ambiguous_duplicates.update(group)
            continue
        preferred = min(
            group,
            key=lambda name: (
                _metadata_hint(name), names.index(name)
            ),
        )
        duplicates.update(name for name in group if name != preferred)

    prompt: list[str] = []
    answer: list[str] = []
    ignored: list[str] = []
    unresolved: list[str] = []
    for name in names:
        populated = sum(bool(value) for value in values[name])
        front_hits = len(front_positions[name])
        back_hits = len(back_positions[name])
        if name in duplicates:
            ignored.append(name)
        elif name in ambiguous_duplicates or populated == 0:
            unresolved.append(name)
        elif name in split_prompt:
            prompt.append(name)
        elif name in split_answer:
            answer.append(name)
        elif front_hits == populated and front_hits >= 2:
            prompt.append(name)
        elif _metadata_evidence(name, values[name]) and front_hits == 0:
            ignored.append(name)
        elif back_hits == populated and front_hits == 0 and back_hits >= 2:
            answer.append(name)
        else:
            unresolved.append(name)

    varied_prompt = [name for name in prompt if len(set(values[name])) > 1]
    unresolved.extend(name for name in prompt if name not in varied_prompt)
    prompt = varied_prompt

    if not prompt or not answer:
        return FieldLayoutSuggestion(
            None,
            "low",
            ("No clear prompt and answer fields were confirmed across the cards.",),
            tuple(unresolved),
        )
    if len(unresolved) > max(3, len(names) // 3):
        return FieldLayoutSuggestion(
            None,
            "low",
            ("Too many fields could not be classified safely; keep editing manually.",),
            tuple(unresolved),
        )

    def order(name: str, positions: dict[str, list[int]]) -> tuple[float, int]:
        hits = positions[name]
        return sum(hits) / len(hits), names.index(name)

    prompt.sort(key=lambda name: order(name, front_positions))
    answer.sort(key=lambda name: order(name, back_positions))
    reasons = (
        f"Compared {len(samples)} cards from this template.",
        f"Matched {len(prompt)} prompt and {len(answer)} answer fields to rendered sides.",
    )
    if unresolved:
        reasons += (f"Left {len(unresolved)} uncertain fields on Auto.",)
    return FieldLayoutSuggestion(
        TemplateFieldProfile(tuple(prompt), tuple(answer), tuple(ignored)),
        "high",
        reasons,
        tuple(unresolved),
    )


def _shared_word_ratio(front: str, back: str) -> float:
    front_words = set(re.findall(r"\w+", front.casefold()))
    if not front_words:
        return 0.0
    back_words = set(re.findall(r"\w+", back.casefold()))
    return len(front_words & back_words) / len(front_words)


def _suggest_field_profile(
    raw: RawCardContent,
    presentation: CardPresentation,
) -> TemplateFieldProfile | None:
    if len(raw.fields) < 3:
        return None
    if not all(
        len(side.sections) == 1 and side.sections[0].id.endswith(":fallback")
        for side in (presentation.front, presentation.back)
    ):
        return None
    if _ANSWER_RULE.search(raw.back_html) or raw.back_html.startswith(raw.front_html):
        return None
    if not re.search(r"<(?:script|details)\b", raw.front_html + raw.back_html, re.IGNORECASE):
        return None

    front = _render_document(raw.front_html, raw.front_av)
    back = _render_document(raw.back_html, raw.back_av)
    if _shared_word_ratio(front.text, back.text) < 0.5:
        return None
    unique_fields: list[tuple[SourceField, _RenderedDocument]] = []
    seen_content: set[str] = set()
    for field in raw.fields:
        rendered = _render_document(field.html)
        content_key = _reconcile_key(rendered.text)
        if not content_key or content_key in seen_content:
            continue
        seen_content.add(content_key)
        unique_fields.append((field, rendered))

    prompt = next(
        (
            (field, rendered)
            for field, rendered in unique_fields
            if _field_is_present(rendered, front)
        ),
        None,
    )
    if prompt is None:
        return None
    prompt_key = _reconcile_key(prompt[1].text)
    answer_fields = tuple(
        field.name
        for field, rendered in unique_fields
        if _reconcile_key(rendered.text) != prompt_key
        and _field_is_present(rendered, back)
    )
    if not answer_fields:
        return None
    return TemplateFieldProfile((prompt[0].name,), answer_fields)


def _field_side(
    side: str,
    names: tuple[str, ...],
    fields: tuple[SourceField, ...],
    *,
    auto_names: frozenset[str] = frozenset(),
    rendered_side: _RenderedDocument | None = None,
    excluded_auto_content: frozenset[str] = frozenset(),
    card_css: str = "",
) -> CardSide:
    available = {field.name: field for field in fields}
    seen: dict[str, int] = {}
    seen_auto_content = set(excluded_auto_content)
    sections: list[PresentationSection] = []
    for name in names:
        field = available.get(name)
        if field is None:
            continue
        rendered = _render_document(field.html, card_css=card_css)
        text = rendered.text
        if not text:
            continue
        content_key = _reconcile_key(text)
        if name in auto_names:
            if rendered_side is None or not _field_is_present(rendered, rendered_side):
                continue
            if content_key in seen_auto_content:
                continue
        seen_auto_content.add(content_key)
        sections.append(
            PresentationSection(
                _unique_id(f"{side}:field", name, seen),
                text,
                source_label=name,
                underlines=rendered.underlines,
                furigana=rendered.furigana,
            )
        )
    if sections:
        return CardSide(tuple(sections))
    label = "Question" if side == "front" else "Answer"
    return CardSide((PresentationSection(f"{side}:fallback", "(empty card)", label),))


def _present_fields(
    raw: RawCardContent,
    profile: TemplateFieldProfile,
    *,
    suggested: bool = False,
) -> CardPresentation:
    front = _field_side(
        "front", profile.prompt_fields, raw.fields, card_css=raw.card_css
    )
    assigned = set(
        profile.prompt_fields + profile.answer_fields + profile.ignored_fields
    )
    auto_fields = tuple(field.name for field in raw.fields if field.name not in assigned)
    front_content = frozenset(_reconcile_key(section.text) for section in front.sections)
    return CardPresentation(
        raw.identity,
        front,
        _field_side(
            "back",
            profile.answer_fields + auto_fields,
            raw.fields,
            auto_names=frozenset(auto_fields),
            rendered_side=_render_document(raw.back_html, raw.back_av),
            excluded_auto_content=front_content,
            card_css=raw.card_css,
        ),
        profile if suggested else None,
    )


def default_field_profile(
    raw: RawCardContent,
    presentation: CardPresentation,
) -> TemplateFieldProfile | None:
    """Build a safe starting profile for a user-opened field editor."""
    available = {field.name for field in raw.fields}

    def source_fields(side: CardSide) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                section.source_label
                for section in side.sections
                if section.source_label in available
            )
        )

    populated = tuple(
        field.name for field in raw.fields if _render_document(field.html).text
    )
    prompt = source_fields(presentation.front)
    if not prompt and populated:
        prompt = (populated[0],)
    answer = tuple(
        name for name in source_fields(presentation.back) if name not in prompt
    )
    if not answer:
        answer = tuple(name for name in populated if name not in prompt)[:1]
    return TemplateFieldProfile(prompt, answer) if prompt and answer else None


def present_card(
    raw: RawCardContent,
    profile: TemplateFieldProfile | None = None,
) -> CardPresentation:
    """Convert one rendered Anki card to a complete immutable presentation."""
    if profile is not None:
        available = {field.name for field in raw.fields}
        selected = profile.prompt_fields + profile.answer_fields
        if profile.prompt_fields and profile.answer_fields and all(
            name in available for name in selected
        ):
            return _present_fields(raw, profile)
    front = _present_side("front", raw.front_html, raw.fields, raw.front_av)
    back_html, used_answer_marker = _strip_answer_html(raw.back_html)
    used_exact_front = False
    if not used_answer_marker and back_html.startswith(raw.front_html):
        back_html = back_html[len(raw.front_html) :]
        used_exact_front = True
    back = _present_side("back", back_html, raw.fields, raw.back_av)
    if not used_answer_marker and not used_exact_front:
        back = _strip_plain_front(back, front)
    presentation = CardPresentation(raw.identity, front, back)
    suggestion = _suggest_field_profile(raw, presentation)
    return _present_fields(raw, suggestion, suggested=True) if suggestion else presentation


def html_to_text(html: str, *, answer: bool = False) -> str:
    """Render standalone Anki HTML with readings inline for plain-text consumers."""
    source = _strip_answer_html(html)[0] if answer else html
    document = _render_document(source)
    text = document.text
    for _start, end, reading in sorted(set(document.furigana), reverse=True):
        text = f"{text[:end]}（{reading}）{text[end:]}"
    return text or "(empty card)"
