"""Portable colored-character previews for local Anki card pictures."""

from __future__ import annotations

import warnings
from collections.abc import Callable
from contextlib import suppress
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image, ImageOps
from rich.style import Style
from rich.text import Text

_MAX_PIXELS = 20_000_000
_DOTS = ((0x01, 0x02, 0x04, 0x40), (0x08, 0x10, 0x20, 0x80))


class ImagePreviewError(ValueError):
    """The referenced picture cannot be safely previewed."""


def _colour(pixels: list[tuple[int, int, int]]) -> tuple[int, int, int]:
    return tuple(sum(pixel[channel] for pixel in pixels) // len(pixels) for channel in range(3))


def _distance(first: tuple[int, int, int], second: tuple[int, int, int]) -> int:
    return sum((left - right) ** 2 for left, right in zip(first, second, strict=True))


def _cell(pixels: list[tuple[int, int, int]]) -> tuple[str, Style]:
    first, second = max(
        ((left, right) for left in pixels for right in pixels),
        key=lambda pair: _distance(*pair),
    )
    if _distance(first, second) < 900:
        background = _colour(pixels)
        foreground = background
        dots = 0
    else:
        groups: list[list[tuple[int, int, int]]] = [[], []]
        dots = 0
        for index, pixel in enumerate(pixels):
            group = int(_distance(pixel, second) < _distance(pixel, first))
            groups[group].append(pixel)
            if group:
                dots |= _DOTS[index % 2][index // 2]
        if len(groups[1]) > len(groups[0]):
            dots ^= 0xFF
            groups.reverse()
        foreground = _colour(groups[1])
        background = _colour(groups[0])
    fg = f"#{foreground[0]:02x}{foreground[1]:02x}{foreground[2]:02x}"
    bg = f"#{background[0]:02x}{background[1]:02x}{background[2]:02x}"
    return chr(0x2800 + dots) if dots else " ", Style(color=fg, bgcolor=bg)


@lru_cache(maxsize=64)
def _render(path: Path, modified_ns: int, file_size: int, width: int, rows: int) -> Text:
    del modified_ns, file_size  # Cache keys invalidate when the media file changes.
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as opened:
                if opened.width * opened.height > _MAX_PIXELS:
                    raise ImagePreviewError("picture is too large")
                picture = ImageOps.exif_transpose(opened).convert("RGBA")
                background = Image.new("RGBA", picture.size, "white")
                background.alpha_composite(picture)
                picture = background.convert("RGB")
                picture.thumbnail((width * 2, rows * 4), Image.Resampling.LANCZOS)
    except ImagePreviewError:
        raise
    except Exception as exc:
        raise ImagePreviewError("picture could not be decoded") from exc

    columns = max(1, (picture.width + 1) // 2)
    height = max(1, (picture.height + 3) // 4)
    picture = picture.resize((columns * 2, height * 4), Image.Resampling.LANCZOS)
    result = Text(no_wrap=True)
    for row in range(height):
        if row:
            result.append("\n")
        for column in range(columns):
            pixels = [
                picture.getpixel((column * 2 + x, row * 4 + y))
                for y in range(4) for x in range(2)
            ]
            character, style = _cell(pixels)
            result.append(character, style=style)
    return result


def _render_bounded(path: Path, width: int, rows: int, *, max_width: int, max_rows: int) -> Text:
    try:
        stat = path.stat()
        if not path.is_file():
            raise ImagePreviewError("picture is not a file")
    except OSError as exc:
        raise ImagePreviewError("picture is missing") from exc
    columns = max(1, min(width, max_width))
    height = max(1, min(rows, max_rows))
    return _render(path, stat.st_mtime_ns, stat.st_size, columns, height).copy()


def render_image(path: Path, width: int, rows: int) -> Text:
    """Render a compact preview; callers handle ImagePreviewError as card content."""
    return _render_bounded(path, width, rows, max_width=80, max_rows=3)


def render_image_detail(path: Path, width: int, rows: int) -> Text:
    """Render a larger bounded character image for an in-terminal viewport."""
    return _render_bounded(path, width, rows, max_width=160, max_rows=72)


def resolve_local_image(source: str, resolve: Callable[[str], Path]) -> Path:
    """Accept only a local card source, then let the backend confine its path."""
    url = urlsplit(source)
    if url.scheme or url.netloc or not url.path:
        raise ImagePreviewError("picture source is not local")
    return resolve(url.path)


def expand_image_previews(
    document: Text,
    resolve: Callable[[str], Path],
    width: int,
    rows: int,
    native_marks: list[tuple[Path, int, int]] | None = None,
    *,
    media_pending: bool = False,
) -> Text:
    """Insert previews after visible, marked image labels in their card order."""
    marked = []
    for span in document.spans:
        style = Style.parse(span.style) if isinstance(span.style, str) else span.style
        source = style.meta.get("repetui_image")
        if isinstance(source, str) and span.start < span.end:
            marked.append((span.start, span.end, source))
    if not marked:
        return document

    result = Text(overflow="fold")
    cursor = 0
    for start, end, source in sorted(marked):
        if start < cursor:
            continue
        result.append_text(document[cursor:end])
        path = None
        try:
            path = resolve_local_image(source, resolve)
            preview = render_image(path, width, rows)
            if native_marks is not None:
                lines = preview.plain.splitlines()
                native_index = len(native_marks)
                preview.stylize(
                    Style(meta={"repetui_native_picture": native_index}), 0, len(preview)
                )
                native_marks.append((path, max(map(len, lines), default=1), len(lines)))
        except (ImagePreviewError, OSError, ValueError, RuntimeError):
            pending = False
            if media_pending and path is not None:
                with suppress(OSError):
                    pending = not path.is_file()
            preview = Text(
                "[picture downloading]" if pending else "[picture unavailable]",
                style="#d7b85a" if pending else "#dc6b72",
            )
        result.append("\n")
        result.append_text(preview)
        result.append("\n")
        cursor = end
    result.append_text(document[cursor:])
    return result
