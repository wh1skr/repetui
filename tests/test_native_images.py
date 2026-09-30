"""Protocol and layout checks; these do not claim visible terminal pixels."""

import re
from pathlib import Path

import pytest
from PIL import Image
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static

from repetui.native_images import (
    NativeImageOverlay,
    NativePicture,
    kitty_placement_available,
    mark_native_picture,
)


class PictureApp(App[None]):
    def compose(self) -> ComposeResult:
        with VerticalScroll(id="scroll"):
            yield Static(id="card")


@pytest.mark.parametrize(
    ("environment", "available"),
    [
        ({}, False),
        ({"TERM_PROGRAM": "WezTerm"}, False),
        ({"TERM_PROGRAM": "WezTerm", "TERM_PROGRAM_VERSION": "20210203"}, False),
        ({"TERM_PROGRAM": "WezTerm", "TERM_PROGRAM_VERSION": "20240203"}, True),
        ({"TERM_PROGRAM": "WezTerm", "TERM_PROGRAM_VERSION": "20240203", "HERDR_ENV": "1"}, False),
        ({"HERDR_ENV": "1", "REPETUI_NATIVE_IMAGES": "kitty"}, True),
        ({"KITTY_WINDOW_ID": "42", "TMUX": "/tmp/tmux-1000/default,1,0"}, False),
        ({"KITTY_WINDOW_ID": "42"}, True),
        ({"KITTY_WINDOW_ID": "42", "REPETUI_NATIVE_IMAGES": "off"}, False),
    ],
)
def test_native_capability_never_reads_terminal_input(environment, available) -> None:
    assert kitty_placement_available(environment) is available


@pytest.mark.asyncio
async def test_standard_kitty_placement_tracks_wrapped_cells_and_scroll(
    tmp_path: Path,
) -> None:
    path = tmp_path / "tiny.png"
    Image.new("RGB", (1, 1), "red").save(path)
    app = PictureApp()
    async with app.run_test(size=(40, 6)) as pilot:
        card = app.query_one("#card", Static)
        scroll = app.query_one("#scroll", VerticalScroll)
        document = Text("Heading\n" * 5)
        document.append_text(mark_native_picture(Text("####\n####"), 0))
        document.append("\nTail\n" * 5)
        card.update(document)
        await pilot.pause()

        output: list[str] = []
        overlay = NativeImageOverlay(output.append, enabled=True)
        picture = NativePicture(path, 4, 2)
        scroll.scroll_to(y=5, animate=False)
        await pilot.pause()
        overlay.paint(card, scroll, [picture])
        assert any("a=T,f=100" in part for part in output)
        assert not any("a=t," in part for part in output)
        assert re.search(r"\x1b\[1;1H\x1b_Ga=p,i=\d+,c=4,r=2,C=1", output[-1])

        scroll.scroll_to(y=6, animate=False)
        await pilot.pause()
        overlay.paint(card, scroll, [picture])
        assert any("a=d,d=I,i=" in part for part in output)
        assert re.search(r"\x1b\[1;1H\x1b_Ga=p,i=\d+,c=4,r=1,C=1", output[-1])

        scroll.scroll_to(y=12, animate=False)
        await pilot.pause()
        before = len(output)
        overlay.paint(card, scroll, [picture])
        assert len(output) == before + 1  # Scoped delete, no new placement.
        assert "a=d,d=I,i=" in output[-1]


@pytest.mark.asyncio
async def test_failed_output_disables_native_and_leaves_character_picture(
    tmp_path: Path,
) -> None:
    path = tmp_path / "tiny.png"
    Image.new("RGB", (1, 1), "blue").save(path)
    app = PictureApp()
    async with app.run_test(size=(40, 6)) as pilot:
        card = app.query_one("#card", Static)
        card.update(mark_native_picture(Text("AB\nCD"), 0))
        await pilot.pause()

        def fail(_: str) -> None:
            raise OSError("terminal pipe closed")

        overlay = NativeImageOverlay(fail, enabled=True)
        overlay.paint(card, app.query_one("#scroll", VerticalScroll), [NativePicture(path, 2, 2)])
        assert not overlay.enabled
        assert "AB\nCD" in str(card.render())
