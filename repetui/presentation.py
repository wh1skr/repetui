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
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlsplit

from rich.cells import cell_len

_ANSWER_RULE = re.compile(r'<hr[^>]*\bid\s*=\s*["\']?answer["\']?[^>]*>', re.IGNORECASE)
_SOUND = re.compile(r"\[sound:([^\]]+)\]", re.IGNORECASE)
_AV_REFERENCE = re.compile(r"\[anki:play:[^:\]]+:(\d+)\]", re.IGNORECASE)
_TYPE_MARKER = re.compile(r"\[\[type:[^\]]+\]\]", re.IGNORECASE)
_SPACE = re.compile(r"[ \t]+")
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


@dataclass(frozen=True)
class PresentationSection:
    """One ordered section whose content came from the rendered card side."""

    id: str
    text: str
    label: str | None = None
    source_label: str | None = None
    label_is_content: bool = False

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
    label_parts: list[str]
    body_parts: list[str]


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

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.prelude: list[str] = []
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

    def _emit(self, text: str) -> None:
        self.parts.append(text)
        if self._heading_depth and self._active is not None:
            self._active.label_parts.append(text)
        elif self._active is not None:
            self._active.body_parts.append(text)
        else:
            self.prelude.append(text)

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

        classes = self._classes(attributes)
        if tag == "svg":
            self._emit("[math]" if "latex" in classes else "[image]")
            self._ignored_depth = 1
        elif tag in self._HEADINGS:
            self._emit("\n")
            self._active = _RawSection("heading", [], [])
            self.sections.append(self._active)
            self._heading_depth = 1
        elif tag == "repetui-label":
            self._active = _RawSection("label", [], [])
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
        if not self._ignored_depth:
            self._emit(data)


@dataclass(frozen=True)
class _RenderedDocument:
    text: str
    structural_sections: tuple[tuple[str, str, str], ...]
    prelude: str
    atoms: tuple[str, ...]


def _promote_structural_labels(html: str) -> str:
    html = _UNDERLINED_HEADING.sub(lambda match: f"<h6>{match.group('label')}</h6>", html)
    return _INLINE_LABEL.sub(
        lambda match: f"<repetui-label>{match.group('label')}:</repetui-label>", html
    )


def _render_document(html: str, av: tuple[AVReference, ...] = ()) -> _RenderedDocument:
    parser = _RenderedHTMLParser()
    parser.feed(_promote_structural_labels(html))
    parser.close()
    text = _normalise("".join(parser.parts), av)
    sections = tuple(
        (
            section.kind,
            _normalise("".join(section.label_parts), av),
            _normalise("".join(section.body_parts), av),
        )
        for section in parser.sections
    )
    atoms = tuple(
        atom
        for part in parser.parts
        if (atom := _normalise(part, av))
    )
    return _RenderedDocument(
        text,
        sections,
        _normalise("".join(parser.prelude), av),
        atoms,
    )


def _normalise(text: str, av: tuple[AVReference, ...] = ()) -> str:
    text = text.replace("\xa0", " ")
    text = _SOUND.sub(lambda match: f"[audio: {_media_name(match.group(1))}]", text)

    def replace_av(match: re.Match[str]) -> str:
        index = int(match.group(1))
        return av[index].placeholder if index < len(av) else "[audio]"

    text = _AV_REFERENCE.sub(replace_av, text)
    text = _TYPE_MARKER.sub("[type answer]", text)

    lines: list[str] = []
    in_fence = False
    for raw_line in text.splitlines():
        if raw_line.strip() in {"```text", "```"}:
            line = raw_line.strip()
            in_fence = line != "```"
        elif in_fence:
            line = raw_line.rstrip()
        else:
            line = _SPACE.sub(" ", raw_line).strip()
        if line:
            lines.append(line)
        elif lines and lines[-1] != "":
            lines.append("")
    return "\n".join(lines).strip()


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


def _structural_sections(side: str, document: _RenderedDocument) -> tuple[PresentationSection, ...]:
    if not document.structural_sections:
        return ()
    seen: dict[str, int] = {}
    result: list[PresentationSection] = []
    if document.prelude:
        result.append(PresentationSection(f"{side}:preamble", document.prelude))
    for kind, raw_label, body in document.structural_sections:
        label = raw_label.removesuffix(":").strip()
        if label or body:
            if kind == "label" or raw_label.rstrip().endswith(":"):
                text = _normalise(f"{raw_label} {body}")
                result.append(
                    PresentationSection(
                        _unique_id(f"{side}:label", label, seen),
                        text,
                        label=label or None,
                    )
                )
            else:
                result.append(
                    PresentationSection(
                        _unique_id(f"{side}:heading", label, seen),
                        body,
                        label=label or None,
                        label_is_content=True,
                    )
                )
    joined = "\n\n".join(section.display_text for section in result)
    return tuple(result) if _reconcile_key(joined) == _reconcile_key(document.text) else ()


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
    return sections if _reconcile_key(joined) == _reconcile_key(document.text) else ()


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
        result.append(
            PresentationSection(
                section.id,
                section.text,
                section.label,
                source_label,
                section.label_is_content,
            )
        )
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
    return CardSide((PresentationSection(f"{side}:fallback", document.text, label),))


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
            return CardSide((PresentationSection("back:fallback", remainder, "Answer"),))
    return back


def _terminal_width(text: str) -> int:
    return cell_len(text)


def _field_is_present(field: _RenderedDocument, side: _RenderedDocument) -> bool:
    key = _reconcile_key(field.text)
    if len(key) <= 2 and key.isascii() and key.isalnum():
        return any(_reconcile_key(atom) == key for atom in side.atoms)
    return key in _reconcile_key(side.text)


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
) -> CardSide:
    available = {field.name: field for field in fields}
    seen: dict[str, int] = {}
    seen_auto_content = set(excluded_auto_content)
    sections: list[PresentationSection] = []
    for name in names:
        field = available.get(name)
        if field is None:
            continue
        rendered = _render_document(field.html)
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
    front = _field_side("front", profile.prompt_fields, raw.fields)
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
    """Render standalone Anki HTML with the card-presentation policy."""
    source = _strip_answer_html(html)[0] if answer else html
    return _render_document(source).text or "(empty card)"
