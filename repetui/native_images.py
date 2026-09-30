"""Optional Kitty graphics overlay anchored to Textual's rendered image spans.

Character previews remain in the document. Native pixels are painted over their
visible cells after Textual refreshes, so a missing protocol leaves a useful card.
"""

from __future__ import annotations

import base64
import io
import os
import secrets
import warnings
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageOps
from rich.cells import cell_len
from rich.style import Style
from rich.text import Text
from textual.app import App
from textual.geometry import Region
from textual.widget import Widget

from .images import _MAX_PIXELS, ImagePreviewError


@dataclass(frozen=True)
class NativePicture:
    path: Path
    columns: int
    rows: int


def kitty_placement_available(environ: Mapping[str, str] | None = None) -> bool:
    """Choose only a known Kitty placement path; never query or consume stdin."""
    env = os.environ if environ is None else environ
    choice = env.get("REPETUI_NATIVE_IMAGES", "auto").lower()
    if choice == "off":
        return False
    if choice == "kitty":
        return True
    if choice != "auto" or any(env.get(name) for name in ("HERDR_ENV", "TMUX", "STY")):
        # Multiplexer graphics forwarding varies and needs opt-in.
        return False
    if env.get("KITTY_WINDOW_ID"):
        return True
    if env.get("TERM_PROGRAM", "").lower() == "wezterm":
        version = env.get("TERM_PROGRAM_VERSION", "")[:8]
        return version.isdecimal() and version >= "20220319"
    return False


def mark_native_picture(picture: Text, index: int) -> Text:
    """Tag character cells so the native overlay can use actual Textual layout."""
    tagged = picture.copy()
    tagged.stylize(Style(meta={"repetui_native_picture": index}), 0, len(tagged))
    return tagged


@lru_cache(maxsize=8)
def _decoded_pixels(
    path: Path, modified_ns: int, file_size: int, columns: int, rows: int
) -> Image.Image:
    del modified_ns, file_size  # Cache keys invalidate when a media file changes.
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as opened:
                if opened.width * opened.height > _MAX_PIXELS:
                    raise ImagePreviewError("picture is too large")
                picture = ImageOps.exif_transpose(opened).convert("RGBA")
                white = Image.new("RGBA", picture.size, "white")
                white.alpha_composite(picture)
                return white.convert("RGB").resize(
                    (columns * 8, rows * 16), Image.Resampling.LANCZOS
                )
    except ImagePreviewError:
        raise
    except Exception as exc:
        raise ImagePreviewError("picture could not be decoded") from exc


def _picture_pixels(path: Path, columns: int, rows: int) -> Image.Image:
    stat = path.stat()
    return _decoded_pixels(path, stat.st_mtime_ns, stat.st_size, columns, rows)


def _marked_regions(widget: Widget, count: int) -> list[Region | None]:
    """Find marked cells after Textual has wrapped the Rich document."""
    bounds: list[list[int] | None] = [None] * count
    if not widget.size.width or not widget.size.height:
        return [None] * count
    for y, line in enumerate(widget.render_lines(Region(0, 0, *widget.size))):
        x = 0
        for segment in line:
            width = cell_len(segment.text)
            index = (
                (segment.style.meta or {}).get("repetui_native_picture")
                if segment.style else None
            )
            if isinstance(index, int) and 0 <= index < count and width:
                box = bounds[index]
                if box is None:
                    bounds[index] = [x, y, x + width, y + 1]
                else:
                    box[0] = min(box[0], x)
                    box[1] = min(box[1], y)
                    box[2] = max(box[2], x + width)
                    box[3] = max(box[3], y + 1)
            x += width
    return [
        Region(box[0], box[1], box[2] - box[0], box[3] - box[1]) if box else None
        for box in bounds
    ]


class NativeImageOverlay:
    """Paint scoped standard Kitty placements over character-image rectangles."""

    def __init__(self, write: Callable[[str], None], *, enabled: bool) -> None:
        self.write = write
        self.enabled = enabled
        self._ids: list[int] = []
        self._next_id = secrets.randbelow(2**31 - 1) + 1

    def clear(self) -> None:
        ids, self._ids = self._ids, []
        for image_id in ids:
            try:
                self.write(f"\x1b_Ga=d,d=I,i={image_id},q=2\x1b\\")
            except Exception:
                self.enabled = False
                break

    def _place(self, picture: Image.Image, target: Region) -> None:
        image_id = self._next_id
        self._next_id = (image_id % (2**31 - 1)) + 1
        buffer = io.BytesIO()
        picture.save(buffer, format="PNG")
        payload = base64.b64encode(buffer.getvalue()).decode("ascii")
        self._ids.append(image_id)  # Include partial transmissions in later cleanup.
        for offset in range(0, len(payload), 4096):
            chunk = payload[offset : offset + 4096]
            more = int(offset + 4096 < len(payload))
            header = (
                f"a=T,f=100,i={image_id},q=2,m={more}"
                if offset == 0 else f"i={image_id},q=2,m={more}"
            )
            self.write(f"\x1b_G{header};{chunk}\x1b\\")
        self.write(
            f"\x1b7\x1b[{target.y + 1};{target.x + 1}H"
            f"\x1b_Ga=p,i={image_id},c={target.width},r={target.height},C=1,q=2\x1b\\"
            "\x1b8"
        )

    def paint(self, widget: Widget, viewport: Widget, pictures: Sequence[NativePicture]) -> None:
        if not self.enabled:
            return
        self.clear()
        if not self.enabled or not pictures:
            return
        clip = viewport.scrollable_content_region.intersection(widget.screen.region)
        try:
            for picture, local in zip(
                pictures, _marked_regions(widget, len(pictures)), strict=True
            ):
                if (
                    local is None
                    or local.width != picture.columns
                    or local.height != picture.rows
                ):
                    # A changed wrap cannot safely map pixels to source cells.
                    continue
                full = Region(
                    widget.region.x + local.x, widget.region.y + local.y,
                    local.width, local.height,
                )
                visible = full.intersection(clip)
                if not visible.width or not visible.height:
                    continue
                pixels = _picture_pixels(picture.path, full.width, full.height)
                source = (
                    (visible.x - full.x) * 8,
                    (visible.y - full.y) * 16,
                    (visible.right - full.x) * 8,
                    (visible.bottom - full.y) * 16,
                )
                self._place(pixels.crop(source), visible)
        except Exception:
            # Optional graphics must never interrupt the character review path.
            self.clear()
            self.enabled = False


def overlay_for_app(app: App[object]) -> NativeImageOverlay:
    driver = getattr(app, "_driver", None)
    write = getattr(driver, "write", None)
    enabled = not app.is_headless and kitty_placement_available() and callable(write)
    return NativeImageOverlay(write if callable(write) else lambda _: None, enabled=enabled)
