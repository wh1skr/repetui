from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

from repetui.backend import AnkiBackend, DueCounts
from repetui.flow import SectionState, compose_review
from repetui.images import (
    ImagePreviewError,
    expand_image_previews,
    render_image,
    render_image_detail,
    resolve_local_image,
)
from repetui.preferences import SectionMode
from repetui.presentation import CardTemplateIdentity, RawCardContent, present_card


def media_backend(folder: Path) -> AnkiBackend:
    backend = AnkiBackend.__new__(AnkiBackend)
    backend._collection = SimpleNamespace(media=SimpleNamespace(dir=lambda: str(folder)))
    return backend


def picture(path: Path) -> None:
    image = Image.new("RGB", (32, 24), "#eee7d7")
    ImageDraw.Draw(image).rectangle((4, 3, 27, 20), fill="#db482b")
    image.save(path)


def review(front: str, back: str = "answer", *, revealed: bool = False):
    card = present_card(RawCardContent(
        CardTemplateIdentity(1, "Pictures", 0, "Card"), front, back,
    ))
    return compose_review(
        card, "Pictures", DueCounts(1, 0, 0), 40,
        revealed=revealed,
        sections=tuple(SectionState(section, SectionMode.SHOW) for section in card.back.sections),
    )


def test_portable_preview_is_bounded_and_colored(tmp_path: Path) -> None:
    path = tmp_path / "sample.png"
    picture(path)

    preview = render_image(path, 40, 3)

    assert 1 <= len(preview.plain.splitlines()) <= 3
    assert all(len(line) <= 40 for line in preview.plain.splitlines())
    assert any(getattr(span.style, "bgcolor", None) for span in preview.spans)


def test_detail_renders_more_than_three_rows_from_disposable_picture(tmp_path: Path) -> None:
    path = tmp_path / "large.png"
    Image.new("RGB", (640, 480), "#b73524").save(path)

    detail = render_image_detail(path, 80, 24)

    assert 3 < len(detail.plain.splitlines()) <= 24
    assert 40 < max(len(line) for line in detail.plain.splitlines()) <= 80
    assert any(getattr(span.style, "bgcolor", None) for span in detail.spans)


def test_detail_source_resolution_reuses_local_media_rules(tmp_path: Path) -> None:
    picture(tmp_path / "one pic.png")
    backend = media_backend(tmp_path)

    assert resolve_local_image("one%20pic.png?cache=1", backend.media_path) == (
        tmp_path / "one pic.png"
    )
    with pytest.raises(ImagePreviewError):
        resolve_local_image("https://example.com/image.png", backend.media_path)
    with pytest.raises(ValueError):
        resolve_local_image("%2e%2e%2foutside.png", backend.media_path)


def test_encoded_names_and_multiple_images_preserve_surrounding_text(tmp_path: Path) -> None:
    picture(tmp_path / "one # pic.png")
    picture(tmp_path / "two.png")
    backend = media_backend(tmp_path)
    document = review('Before <img src="one%20%23%20pic.png"> between <img src="two.png"> after')

    result = expand_image_previews(document, backend.media_path, 40, 3)

    assert result.plain.index("Before") < result.plain.index("[image: one%20%23%20pic.png]")
    assert result.plain.index("[image: one%20%23%20pic.png]") < result.plain.index("between")
    assert result.plain.index("between") < result.plain.index("[image: two.png]")
    assert result.plain.index("[image: two.png]") < result.plain.index("after")
    assert "[picture unavailable]" not in result.plain
    assert sum(bool(getattr(span.style, "bgcolor", None)) for span in result.spans) > 4


@pytest.mark.parametrize("source", [
    "../outside.png", "%2e%2e%2foutside.png", "/tmp/outside.png",
    "https://example.com/outside.png", "file:///tmp/outside.png",
    "link.png",
])
def test_unsafe_image_source_never_opens_outside_media_folder(
    tmp_path: Path, source: str, monkeypatch,
) -> None:
    media = tmp_path / "collection.media"
    media.mkdir()
    outside = tmp_path / "outside.png"
    picture(outside)
    (media / "link.png").symlink_to(outside)
    backend = media_backend(media)
    opened: list[Path] = []

    def record(path: Path, width: int, rows: int):
        opened.append(path)
        raise AssertionError("unsafe file was opened")

    monkeypatch.setattr("repetui.images.render_image", record)
    result = expand_image_previews(review(f'<img src="{source}">'), backend.media_path, 40, 3)

    assert opened == []
    assert "[picture unavailable]" in result.plain


def test_missing_or_corrupt_image_has_a_readable_placeholder(tmp_path: Path) -> None:
    (tmp_path / "broken.png").write_bytes(b"not an image")
    backend = media_backend(tmp_path)
    document = review('<img src="missing.png"> then <img src="broken.png">')

    result = expand_image_previews(document, backend.media_path, 40, 3)

    assert result.plain.count("[picture unavailable]") == 2
    assert "then" in result.plain
    with pytest.raises(ImagePreviewError):
        render_image(tmp_path / "broken.png", 40, 3)


def test_pending_media_only_marks_missing_local_files_as_downloading(tmp_path):
    (tmp_path / "broken.png").write_bytes(b"not an image")
    backend = media_backend(tmp_path)
    document = review(
        '<img src="missing.png"><img src="broken.png">'
        '<img src="https://example.com/remote.png"><img src="../outside.png">'
    )
    pending = expand_image_previews(document, backend.media_path, 40, 3, media_pending=True)
    assert pending.plain.count("[picture downloading]") == 1
    assert pending.plain.count("[picture unavailable]") == 3
    picture(tmp_path / "missing.png")
    arrived = expand_image_previews(document, backend.media_path, 40, 3, media_pending=True)
    assert "[picture downloading]" not in arrived.plain
    assert arrived.plain.count("[picture unavailable]") == 3


def test_image_without_source_or_with_malformed_url_is_nonfatal(tmp_path: Path) -> None:
    backend = media_backend(tmp_path)
    document = review('<img alt="first"><img src="http://[broken" alt="second">')

    result = expand_image_previews(document, backend.media_path, 40, 3)

    assert result.plain.count("[picture unavailable]") == 2


def test_answer_picture_is_inserted_only_after_reveal(tmp_path: Path) -> None:
    picture(tmp_path / "front.png")
    picture(tmp_path / "back.png")
    backend = media_backend(tmp_path)
    front = '<img src="front.png">'
    back = '<hr id=answer><img src="back.png">'

    hidden = expand_image_previews(review(front, back), backend.media_path, 40, 3)
    shown = expand_image_previews(review(front, back, revealed=True), backend.media_path, 40, 3)

    assert "[image: back.png]" not in hidden.plain
    assert "[image: back.png]" in shown.plain
    assert shown.plain.count("[picture unavailable]") == 0
