from pathlib import Path

import pytest

from repetui.app import DeckScreen, HelpScreen, RepetuiApp, ReviewScreen
from repetui.backend import Deck, DueCounts, ReviewCard
from repetui.config import ProfilePaths
from repetui.presentation import CardTemplateIdentity, RawCardContent, present_card


class FakeBackend:
    def __init__(self) -> None:
        self.is_open = False
        self.rating = None
        self.card_available = True

    def open(self) -> None:
        self.is_open = True

    def close(self) -> None:
        self.is_open = False

    def decks(self) -> list[Deck]:
        return [Deck(1, "Japanese", 0, DueCounts(2, 1, 7))]

    def begin_review(self, deck_id: int) -> None:
        assert deck_id == 1

    def counts(self) -> DueCounts:
        return DueCounts(2, 1, 7) if self.card_available else DueCounts(0, 0, 0)

    def next_card(self) -> ReviewCard | None:
        if self.card_available:
            identity = CardTemplateIdentity(1, "Basic", 0, "Card 1")
            presentation = present_card(RawCardContent(identity, "question", "answer"))
            return ReviewCard(42, presentation)
        return None

    def answer(self, rating: int) -> None:
        self.rating = rating
        self.card_available = False


def make_app() -> tuple[RepetuiApp, FakeBackend]:
    backend = FakeBackend()
    profile = ProfilePaths(Path("/tmp"), "test", Path("/tmp/collection.anki2"))
    return RepetuiApp(backend, profile), backend


@pytest.mark.asyncio
async def test_complete_keyboard_review_loop() -> None:
    app, backend = make_app()

    async with app.run_test(size=(70, 20)) as pilot:
        assert isinstance(app.screen, DeckScreen)
        await pilot.press("enter")
        assert isinstance(app.screen, ReviewScreen)
        assert "question" in str(app.screen.query_one("#card").render())
        assert "answer" not in str(app.screen.query_one("#card").render())

        await pilot.press("enter")
        review = app.screen
        assert isinstance(review, ReviewScreen)
        assert review.revealed is True

        await pilot.press("enter")
        assert backend.rating == 3
        assert "Nothing due" in str(review.query_one("#card").render())


@pytest.mark.asyncio
async def test_help_is_hidden_until_requested() -> None:
    app, _ = make_app()

    async with app.run_test(size=(70, 20)) as pilot:
        assert isinstance(app.screen, DeckScreen)
        await pilot.press("?")
        assert isinstance(app.screen, HelpScreen)
        await pilot.press("escape")
        assert isinstance(app.screen, DeckScreen)
