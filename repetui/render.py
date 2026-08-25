"""Small, deterministic HTML-to-terminal renderer for Anki cards."""

from __future__ import annotations

import re
from html.parser import HTMLParser

_ANSWER_RULE = re.compile(
    r'<hr[^>]*\bid\s*=\s*["\']?answer["\']?[^>]*>', re.IGNORECASE
)
_SOUND = re.compile(r"\[sound:[^\]]+\]", re.IGNORECASE)


class _CardHTMLParser(HTMLParser):
    """Preserve readable structure while dropping browser-only card content."""

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
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "tr",
        "ul",
    }
    _SUPPRESSED = {"script", "style", "svg", "template"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._suppressed_depth = 0
        self._hidden_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag in self._SUPPRESSED:
            self._suppressed_depth += 1
            return
        if self._suppressed_depth:
            return
        if "hidden" in attributes or attributes.get("aria-hidden") == "true":
            self._hidden_depth += 1
            return
        if self._hidden_depth:
            return
        if tag == "br":
            self.parts.append("\n")
        elif tag == "hr":
            self.parts.append("\n────────\n")
        elif tag == "li":
            self.parts.append("\n• ")
        elif tag in self._BLOCKS:
            self.parts.append("\n")
        elif tag == "rt":
            self.parts.append("（")
        elif tag == "img":
            label = attributes.get("alt") or attributes.get("title")
            self.parts.append(label.strip() if label else "[image]")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SUPPRESSED and self._suppressed_depth:
            self._suppressed_depth -= 1
            return
        if self._suppressed_depth:
            return
        if self._hidden_depth:
            self._hidden_depth -= 1
            return
        if tag == "rt":
            self.parts.append("）")
        elif tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._suppressed_depth and not self._hidden_depth:
            self.parts.append(data)


def _answer_only(html: str) -> str:
    """Remove Anki's duplicated FrontSide from answer HTML when possible."""
    match = _ANSWER_RULE.search(html)
    if match and html[match.end() :].strip():
        return html[match.end() :]
    return html


def html_to_text(html: str, *, answer: bool = False) -> str:
    """Convert rendered card HTML into compact Unicode terminal text."""
    source = _answer_only(html) if answer else html
    parser = _CardHTMLParser()
    parser.feed(source)
    parser.close()

    text = _SOUND.sub("", "".join(parser.parts)).replace("\xa0", " ")
    lines: list[str] = []
    for raw_line in text.splitlines():
        line = re.sub(r"[ \t]+", " ", raw_line).strip()
        if line:
            lines.append(line)
        elif lines and lines[-1] != "":
            lines.append("")
    return "\n".join(lines).strip() or "(empty card)"

