import asyncio
from collections.abc import Callable
from pathlib import Path
from threading import Event

import pytest
from textual.widgets import Input

from repetui.app import (
    DeckScreen,
    ErrorScreen,
    FlagSelectionPill,
    HelpScreen,
    OperationStatusPill,
    RepetuiApp,
    ReviewScreen,
    SyncPopup,
    TemplateSettingsScreen,
    compose_deck_row,
)
from repetui.backend import BackendError, Deck, DueCounts, ReviewCard
from repetui.config import ProfilePaths
from repetui.controls import ReviewAction, ReviewControls
from repetui.deck_tree import VisibleDeckRow
from repetui.preferences import JsonPreferences, SectionMode
from repetui.presentation import (
    CardTemplateIdentity,
    RawCardContent,
    SourceField,
    present_card,
)
from repetui.sync import SyncOutcome, SyncStatus


class FakeBackend:
    def __init__(
        self,
        card_content: RawCardContent | None = None,
        decks: list[Deck] | None = None,
    ) -> None:
        self.is_open = False
        self.rating = None
        self.card_available = True
        self.card_content = card_content
        self._decks = decks or [Deck(1, "Japanese", 0, DueCounts(2, 1, 7))]
        self.begun_deck_ids: list[int] = []
        self.count_calls = 0
        self.undo_calls = 0
        self.undo_available = True
        self.operations: list[str] = []
        self.flags: list[int] = []
        self.fail_operation: str | None = None
        self.fail_refresh_after_operation = False

    def open(self) -> None:
        self.is_open = True

    def close(self) -> None:
        self.is_open = False

    def decks(self) -> list[Deck]:
        return self._decks

    def begin_review(self, deck_id: int) -> None:
        assert any(deck.id == deck_id for deck in self._decks)
        self.begun_deck_ids.append(deck_id)

    def counts(self) -> DueCounts:
        self.count_calls += 1
        if self.fail_refresh_after_operation and (
            self.undo_calls or self.operations or self.flags
        ):
            raise BackendError("private refresh detail")
        return DueCounts(2, 1, 7) if self.card_available else DueCounts(0, 0, 0)

    def next_card(self) -> ReviewCard | None:
        if self.card_available:
            content = self.card_content
            if content is None:
                identity = CardTemplateIdentity(1, "Basic", 0, "Card 1")
                content = RawCardContent(identity, "question", "answer")
            presentation = present_card(content)
            return ReviewCard(42, presentation)
        return None

    def answer(self, rating: int) -> None:
        self.rating = rating
        self.card_available = False

    def undo(self) -> bool:
        self.undo_calls += 1
        if self.fail_operation == "undo":
            raise BackendError("private undo detail")
        if not self.undo_available:
            return False
        self.card_available = True
        return True

    def bury_current(self) -> None:
        if self.fail_operation == "bury":
            raise BackendError("private bury detail")
        self.operations.append("bury")
        self.card_available = False

    def suspend_current(self) -> None:
        if self.fail_operation == "suspend":
            raise BackendError("private suspend detail")
        self.operations.append("suspend")
        self.card_available = False

    def set_current_flag(self, flag: int) -> None:
        if self.fail_operation == "flag":
            raise BackendError("private flag detail")
        self.flags.append(flag)


def make_app(
    tmp_path: Path | None = None,
    card_content: RawCardContent | None = None,
    preferences: JsonPreferences | None = None,
    decks: list[Deck] | None = None,
    syncer: Callable[[ProfilePaths], SyncOutcome] | None = None,
) -> tuple[RepetuiApp, FakeBackend]:
    backend = FakeBackend(card_content, decks)
    profile = ProfilePaths(Path("/tmp"), "test", Path("/tmp/collection.anki2"))
    store = preferences or JsonPreferences(
        (tmp_path or Path("/tmp")) / "preferences.json"
    )
    app = (
        RepetuiApp(backend, profile, store)
        if syncer is None
        else RepetuiApp(backend, profile, store, syncer)
    )
    return app, backend


class TwoCardBackend(FakeBackend):
    def __init__(self) -> None:
        super().__init__()
        self.card_ids = [42, 43]

    def counts(self) -> DueCounts:
        self.count_calls += 1
        return DueCounts(0, 0, len(self.card_ids))

    def next_card(self) -> ReviewCard | None:
        if not self.card_ids:
            return None
        identity = CardTemplateIdentity(1, "Basic", 0, "Card 1")
        content = RawCardContent(
            identity,
            f"question {self.card_ids[0]}",
            f"answer {self.card_ids[0]}",
        )
        return ReviewCard(self.card_ids[0], present_card(content))

    def bury_current(self) -> None:
        self.operations.append("bury")
        self.card_ids.pop(0)

    def suspend_current(self) -> None:
        self.operations.append("suspend")
        self.card_ids.pop(0)


def rendered_text(screen: ReviewScreen) -> str:
    return str(screen.query_one("#card").render())


def japanese_card() -> RawCardContent:
    return RawCardContent(
        CardTemplateIdentity(204, "Japanese", 0, "Recognition"),
        "葬",
        """<hr id=answer>
        <h2>Reading</h2><p>そう</p>
        <h2>Meaning</h2><p>burial; interment</p>
        <h2>Mnemonic</h2><p>Flowers laid upon a grave.</p>
        <h2>Examples</h2><p>葬式 — funeral</p>
        """,
    )


def real_kanji_card() -> RawCardContent:
    front = (
        '<div class="kanji">粋</div>'
        '<div class="quest">Kanji <b>Meaning</b></div>'
        '<div class="input">[[type:sort_id]]</div>'
    )
    long_mnemonic = " ".join(
        "A stylish rice ceremony makes the unusual shape memorable."
        for _ in range(6)
    )
    return RawCardContent(
        CardTemplateIdentity(204, "kanji Model", 0, "Recognition"),
        front,
        front
        + f"""
        <br><span><b>Meaning:</b> Stylish</span><br>
        <span><b>On&apos;yomi:</b></span> すい
        <span><b>Kun&apos;yomi:</b></span> いき<br>
        <span><b>Radicals:</b></span> 米, 九, 十 (rice, nine, cross)<br><br>
        <u><span><b>Meaning Mnemonic</b></span></u><br><br>
        <span>{long_mnemonic}</span><br><br>
        <u><span><b>Reading Mnemonic</b></span></u><br><br>
        <span>すい sounds like a stylish suit.</span>
        """,
        (
            SourceField("meaning", "Stylish"),
            SourceField("on_yomi", "すい"),
            SourceField("kun_yomi", "いき"),
            SourceField("radicals", "米, 九, 十 (rice, nine, cross)"),
            SourceField("meaning_mnemonic", long_mnemonic),
            SourceField("reading_mnemonic", "すい sounds like a stylish suit."),
        ),
    )


@pytest.mark.asyncio
async def test_decks_are_compact_unboxed_and_keep_identity_plus_counts_at_40x6(
    tmp_path,
) -> None:
    decks = [
        Deck(
            1,
            "完全な統計",
            0,
            DueCounts(8, 17, 213),
        ),
        Deck(2, "完全な統計::日本語", 1, DueCounts(3, 4, 20)),
        Deck(3, "AWS", 0, DueCounts(3, 0, 12)),
    ]
    app, _ = make_app(tmp_path, decks=decks)

    async with app.run_test(size=(40, 6)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, DeckScreen)
        assert str(screen.query_one("#deck-header").render()) == "decks · repetui 0.1.1"
        assert screen.query_one("#deck-header").region.y == 0
        assert len(screen.query("#logo")) == 0
        assert len(screen.query(".quiet-footer")) == 0

        items = list(screen.query("DeckItem"))
        assert len(items) == 2
        first = items[0]
        row = str(first.query_one(".deck-row").render())
        assert row == compose_deck_row(VisibleDeckRow(decks[0], True, False), 40).plain
        assert row.startswith("▸ 完全な統計")
        assert "238" in row
        assert "8/17/213" in row
        assert first.region.y == 1

        await pilot.press("tab")
        child = list(screen.query("DeckItem"))[1]
        assert str(child.query_one(".deck-row").render()).startswith("> 日本語")

        await pilot.resize_terminal(14, 6)
        row = str(child.query_one(".deck-row").render())
        assert "日本語" in row
        assert "27" in row
        assert "3/4/20" not in row

        await pilot.resize_terminal(11, 6)
        row = str(child.query_one(".deck-row").render())
        assert row.startswith("> 日本語")
        assert "27" not in row

        await pilot.resize_terminal(8, 6)
        assert "日本語" in str(child.query_one(".deck-row").render())

        await pilot.resize_terminal(4, 6)
        narrow = str(child.query_one(".deck-row").render())
        assert narrow.startswith("日")
        assert "…" in narrow


@pytest.mark.asyncio
async def test_tab_expands_and_collapses_selected_parent_without_moving_it(
    tmp_path,
) -> None:
    decks = [
        Deck(1, "Japanese", 0, DueCounts(4, 1, 9)),
        Deck(2, "Japanese::Kanji", 1, DueCounts(2, 0, 3)),
        Deck(3, "Japanese::Kanji::N5", 2, DueCounts(1, 0, 1)),
        Deck(4, "Japanese::Vocabulary", 1, DueCounts(2, 1, 6)),
        Deck(5, "AWS", 0, DueCounts(1, 0, 2)),
    ]
    app, _ = make_app(tmp_path, decks=decks)

    async with app.run_test(size=(40, 6)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, DeckScreen)
        view = screen.query_one("#decks")
        assert [item.deck.id for item in screen.query("DeckItem")] == [1, 5]
        assert str(screen.query("DeckItem")[0].query_one(".deck-row").render()).startswith(
            "▸ Japanese"
        )

        await pilot.press("tab")
        assert [item.deck.id for item in screen.query("DeckItem")] == [1, 2, 4, 5]
        assert view.index == 0
        rendered_rows = [
            str(item.query_one(".deck-row").render())
            for item in screen.query("DeckItem")
        ]
        assert rendered_rows[0].startswith("▾ Japanese")
        assert rendered_rows[1].startswith("> ▸ Kanji")
        assert rendered_rows[2].startswith("> Vocabulary")
        assert "▸" not in rendered_rows[2]
        assert "▾" not in rendered_rows[2]

        await pilot.press("j", "tab")
        assert [item.deck.id for item in screen.query("DeckItem")] == [1, 2, 3, 4, 5]
        assert screen.query("DeckItem")[view.index].deck.id == 2

        await pilot.press("tab")
        assert [item.deck.id for item in screen.query("DeckItem")] == [1, 2, 4, 5]
        assert screen.query("DeckItem")[view.index].deck.id == 2


@pytest.mark.asyncio
async def test_leaf_tab_only_flashes_the_selected_row(tmp_path, monkeypatch) -> None:
    decks = [
        Deck(1, "Japanese", 0, DueCounts(4, 1, 9)),
        Deck(2, "Japanese::語彙", 1, DueCounts(2, 1, 6)),
    ]
    app, _ = make_app(tmp_path, decks=decks)

    async with app.run_test(size=(40, 6)) as pilot:
        screen = app.screen
        assert isinstance(screen, DeckScreen)
        await pilot.press("tab", "j")
        view = screen.query_one("#decks")
        leaf = screen.query("DeckItem")[view.index]
        notifications = []
        monkeypatch.setattr(app, "notify", lambda *args, **kwargs: notifications.append(args))

        screen.action_toggle_deck()

        assert leaf.has_class("-leaf-feedback")
        assert view.index == 1
        assert len(app.screen_stack) == 2
        assert notifications == []
        await pilot.pause(0.25)
        assert not leaf.has_class("-leaf-feedback")


@pytest.mark.asyncio
async def test_enter_reviews_selected_parent_and_leaf(tmp_path) -> None:
    decks = [
        Deck(1, "Japanese", 0, DueCounts(4, 1, 9)),
        Deck(2, "Japanese::Kanji", 1, DueCounts(2, 0, 3)),
    ]
    app, backend = make_app(tmp_path, decks=decks)

    async with app.run_test(size=(40, 6)) as pilot:
        await pilot.press("enter")
        assert isinstance(app.screen, ReviewScreen)
        assert backend.begun_deck_ids[-1] == 1

        await pilot.press("escape", "tab", "j", "enter")
        assert isinstance(app.screen, ReviewScreen)
        assert backend.begun_deck_ids[-1] == 2


@pytest.mark.asyncio
async def test_tree_state_survives_review_return_restart_and_backend_refresh(
    tmp_path,
) -> None:
    decks = [
        Deck(1, "Japanese", 0, DueCounts(4, 1, 9)),
        Deck(2, "Japanese::Kanji", 1, DueCounts(2, 0, 3)),
        Deck(3, "AWS", 0, DueCounts(1, 0, 2)),
    ]
    preferences_path = tmp_path / "preferences.json"
    preferences = JsonPreferences(preferences_path)
    app, backend = make_app(tmp_path, preferences=preferences, decks=decks)

    async with app.run_test(size=(40, 6)) as pilot:
        screen = app.screen
        assert isinstance(screen, DeckScreen)
        await pilot.press("tab", "j", "enter")
        assert isinstance(app.screen, ReviewScreen)
        assert backend.begun_deck_ids[-1] == 2

        await pilot.press("escape")
        assert app.screen is screen
        assert [item.deck.id for item in screen.query("DeckItem")] == [1, 2, 3]

        screen.backend_refreshed()
        await pilot.pause()
        assert [item.deck.id for item in screen.query("DeckItem")] == [1, 2, 3]
        assert screen.query("DeckItem")[screen.query_one("#decks").index].deck.id == 2

    restarted, _ = make_app(
        tmp_path,
        preferences=JsonPreferences(preferences_path),
        decks=decks,
    )
    async with restarted.run_test(size=(40, 6)):
        assert [item.deck.id for item in restarted.screen.query("DeckItem")] == [1, 2, 3]


@pytest.mark.asyncio
async def test_sync_reload_keeps_expansion_and_selected_deck(tmp_path) -> None:
    decks = [
        Deck(1, "Japanese", 0, DueCounts(4, 1, 9)),
        Deck(2, "Japanese::Kanji", 1, DueCounts(2, 0, 3)),
        Deck(3, "AWS", 0, DueCounts(1, 0, 2)),
    ]
    app, backend = make_app(
        tmp_path,
        decks=decks,
        syncer=lambda _profile: SyncOutcome(SyncStatus.SYNCED),
    )

    async with app.run_test(size=(40, 6)) as pilot:
        screen = app.screen
        assert isinstance(screen, DeckScreen)
        await pilot.press("tab", "j")

        await pilot.press("s")
        await pilot.pause(1.2)

        assert backend.is_open is True
        assert app.screen is screen
        assert [item.deck.id for item in screen.query("DeckItem")] == [1, 2, 3]
        view = screen.query_one("#decks")
        assert screen.query("DeckItem")[view.index].deck.id == 2


@pytest.mark.asyncio
async def test_complete_keyboard_review_loop(tmp_path) -> None:
    app, backend = make_app(tmp_path)

    async with app.run_test(size=(70, 20)) as pilot:
        assert isinstance(app.screen, DeckScreen)
        await pilot.press("enter")
        assert isinstance(app.screen, ReviewScreen)
        assert "question" in rendered_text(app.screen)
        assert "answer" not in rendered_text(app.screen)
        assert app.screen.query_one("#review-actions").display is False

        await pilot.press("enter")
        review = app.screen
        assert isinstance(review, ReviewScreen)
        assert review.revealed is True
        assert review.query_one("#review-actions").display is True
        assert "answer" in rendered_text(review)
        assert "reveal" not in str(review.query_one("#review-actions").render())

        await pilot.press("enter")
        assert backend.rating == 3
        assert "Nothing due" in str(review.query_one("#card").render())
        assert str(review.query_one("#card").render()).startswith("done · Japanese")


@pytest.mark.asyncio
async def test_undo_restores_the_final_rated_card_and_refreshes_counts(tmp_path) -> None:
    app, backend = make_app(tmp_path)

    async with app.run_test(size=(40, 6)) as pilot:
        await pilot.press("enter", "enter", "3")
        review = app.screen
        assert isinstance(review, ReviewScreen)
        assert review.card is None
        count_calls_before = backend.count_calls

        await pilot.press("u")

        assert isinstance(app.screen, OperationStatusPill)
        assert str(app.screen.query_one(".status-pill").render()) == "[ok] undone"
        assert app.screen_stack[-2] is review
        assert backend.undo_calls == 1
        assert review.card is not None
        assert review.revealed is False
        assert backend.count_calls > count_calls_before


@pytest.mark.asyncio
async def test_saved_review_binding_routes_immediately_after_app_start(tmp_path) -> None:
    preferences = JsonPreferences(tmp_path / "preferences.json")
    controls = ReviewControls.defaults().with_binding(ReviewAction.UNDO, "z")
    profile = ProfilePaths(Path("/tmp"), "test", Path("/tmp/collection.anki2"))
    preferences.set_review_controls(profile, controls)
    app, backend = make_app(tmp_path, preferences=preferences)

    async with app.run_test(size=(40, 6)) as pilot:
        await pilot.press("enter", "enter", "3")
        review = app.screen
        assert isinstance(review, ReviewScreen)
        assert review.card is None

        await pilot.press("u")
        assert app.screen is review
        assert backend.undo_calls == 0

        await pilot.press("z")
        assert isinstance(app.screen, OperationStatusPill)
        assert backend.undo_calls == 1


@pytest.mark.asyncio
async def test_unavailable_undo_shows_concise_feedback_without_changing_card(tmp_path) -> None:
    app, backend = make_app(tmp_path)
    backend.undo_available = False

    async with app.run_test(size=(40, 6)) as pilot:
        await pilot.press("enter")
        review = app.screen
        assert isinstance(review, ReviewScreen)
        current_card = review.card

        await pilot.press("u")

        assert isinstance(app.screen, OperationStatusPill)
        assert str(app.screen.query_one(".status-pill").render()) == (
            "[err] nothing to undo"
        )
        assert review.card is current_card


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "operation", "message"),
    (("b", "bury", "[ok] buried"), ("x", "suspend", "[ok] suspended")),
)
@pytest.mark.parametrize("revealed", [False, True])
async def test_bury_and_suspend_advance_and_refresh_before_or_after_reveal(
    tmp_path, key, operation, message, revealed
) -> None:
    app, backend = make_app(tmp_path)

    async with app.run_test(size=(40, 6)) as pilot:
        await pilot.press("enter")
        review = app.screen
        assert isinstance(review, ReviewScreen)
        if revealed:
            await pilot.press("enter")
        count_calls_before = backend.count_calls

        await pilot.press(key)

        assert isinstance(app.screen, OperationStatusPill)
        assert str(app.screen.query_one(".status-pill").render()) == message
        assert backend.operations == [operation]
        assert review.card is None
        assert backend.count_calls > count_calls_before


@pytest.mark.asyncio
@pytest.mark.parametrize(("key", "operation"), (("b", "bury"), ("x", "suspend")))
async def test_bury_and_suspend_advance_to_the_next_queued_card(
    tmp_path, key, operation
) -> None:
    backend = TwoCardBackend()
    profile = ProfilePaths(Path("/tmp"), "test", Path("/tmp/collection.anki2"))
    app = RepetuiApp(
        backend,
        profile,
        JsonPreferences(tmp_path / "preferences.json"),
    )

    async with app.run_test(size=(40, 6)) as pilot:
        await pilot.press("enter")
        review = app.screen
        assert isinstance(review, ReviewScreen)
        assert review.card is not None and review.card.id == 42

        await pilot.press(key)

        assert backend.operations == [operation]
        assert review.card is not None and review.card.id == 43
        assert "question 43" in rendered_text(review)


@pytest.mark.asyncio
async def test_operation_status_dismisses_after_roughly_one_second(tmp_path) -> None:
    app, _ = make_app(tmp_path)

    async with app.run_test(size=(40, 6)) as pilot:
        await pilot.press("enter")
        review = app.screen
        await pilot.press("b")
        popup = app.screen
        assert isinstance(popup, OperationStatusPill)

        await pilot.pause(0.75)
        assert app.screen is popup
        await pilot.pause(0.3)
        assert app.screen is review


@pytest.mark.asyncio
@pytest.mark.parametrize(("key", "message"), (("0", "[ok] flag clear"), ("3", "[ok] flag 3")))
@pytest.mark.parametrize("revealed", [False, True])
async def test_flag_clear_and_set_keep_the_current_card_and_reveal_state(
    tmp_path, key, message, revealed
) -> None:
    app, backend = make_app(tmp_path)

    async with app.run_test(size=(40, 6)) as pilot:
        await pilot.press("enter")
        review = app.screen
        assert isinstance(review, ReviewScreen)
        if revealed:
            await pilot.press("enter")
        current_card = review.card
        count_calls_before = backend.count_calls

        await pilot.press("f")
        assert isinstance(app.screen, FlagSelectionPill)
        surface = app.screen.query_one(".status-pill")
        assert str(surface.render()) == "[?] flag 0–7 · esc"
        assert surface.region.height == 1
        await pilot.press(key)
        await pilot.pause()

        assert isinstance(app.screen, OperationStatusPill)
        assert str(app.screen.query_one(".status-pill").render()) == message
        assert backend.flags == [int(key)]
        assert review.card is current_card
        assert review.revealed is revealed
        assert backend.count_calls > count_calls_before


@pytest.mark.asyncio
async def test_escape_cancels_flag_selection_without_changing_or_advancing(tmp_path) -> None:
    app, backend = make_app(tmp_path)

    async with app.run_test(size=(40, 6)) as pilot:
        await pilot.press("enter", "enter", "f")
        review = app.screen_stack[-2]
        assert isinstance(review, ReviewScreen)
        current_card = review.card

        await pilot.press("escape")

        assert app.screen is review
        assert review.card is current_card
        assert review.revealed is True
        assert backend.flags == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "keys", "message"),
    (
        ("undo", ("u",), "[err] undo failed"),
        ("bury", ("b",), "[err] bury failed"),
        ("suspend", ("x",), "[err] suspend failed"),
        ("flag", ("f", "3"), "[err] flag failed"),
    ),
)
async def test_backend_operation_failures_keep_the_current_revealed_card(
    tmp_path, operation, keys, message
) -> None:
    app, backend = make_app(tmp_path)
    backend.fail_operation = operation

    async with app.run_test(size=(40, 6)) as pilot:
        await pilot.press("enter", "enter")
        review = app.screen
        assert isinstance(review, ReviewScreen)
        current_card = review.card

        await pilot.press(*keys)
        await pilot.pause()

        assert isinstance(app.screen, OperationStatusPill)
        rendered = str(app.screen.query_one(".status-pill").render())
        assert rendered == message
        assert "private" not in rendered
        assert review.card is current_card
        assert review.revealed is True


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["undo", "bury", "flag"])
async def test_refresh_failure_is_not_misreported_as_an_operation_failure(
    tmp_path, operation
) -> None:
    app, backend = make_app(tmp_path)
    backend.fail_refresh_after_operation = True

    async with app.run_test(size=(40, 6)) as pilot:
        await pilot.press("enter", "enter")
        review = app.screen
        assert isinstance(review, ReviewScreen)
        current_card = review.card

        keys = {"undo": ("u",), "bury": ("b",), "flag": ("f", "3")}[operation]
        await pilot.press(*keys)
        await pilot.pause()

        assert isinstance(app.screen, OperationStatusPill)
        assert str(app.screen.query_one(".status-pill").render()) == (
            "[err] refresh failed"
        )
        if operation == "flag":
            assert review.card is current_card
            assert review.revealed is True
        else:
            assert review.card is None
            assert "question" not in rendered_text(review)
            assert "Could not refresh cards" in rendered_text(review)


@pytest.mark.asyncio
async def test_flow_uses_one_tiny_row_before_reveal_and_three_content_rows_after(
    tmp_path,
) -> None:
    app, _ = make_app(tmp_path)

    async with app.run_test(size=(40, 6)) as pilot:
        await pilot.press("enter")
        review = app.screen
        assert isinstance(review, ReviewScreen)

        front = rendered_text(review)
        assert front.startswith("question  · Card 1")
        assert "Japanese" in front
        assert "10" in front
        assert "2/1/7" in front
        assert "\n" not in front
        assert review.query_one("#review-actions").display is False

        await pilot.press("enter")

        assert rendered_text(review).splitlines()[1] == "answer"
        actions = review.query_one("#review-actions")
        assert str(actions.render()) == "1 again  2 hard  3 good  4 easy"
        assert actions.region.y == 5
        assert actions.region.height == 1


@pytest.mark.asyncio
async def test_real_kanji_card_flows_and_scrolls_at_40_by_6(tmp_path) -> None:
    app, _ = make_app(tmp_path, real_kanji_card())

    async with app.run_test(size=(40, 6)) as pilot:
        await pilot.press("enter")
        review = app.screen
        assert isinstance(review, ReviewScreen)
        front = rendered_text(review)
        assert front.startswith("粋 · Kanji Meaning")
        assert "\n" not in front
        assert "[type answer]" not in front
        assert "Recognition" not in front
        assert "10" in front
        assert "2/1/7" in front

        await pilot.press("enter")
        flow = rendered_text(review)
        assert flow.splitlines()[1].startswith(
            "Meaning: Stylish  ·  On'yomi: すい  ·  Kun'yomi: いき"
        )
        assert "Radicals: 米, 九, 十 (rice, nine, cross)" in flow
        assert "Meaning Mnemonic · A stylish rice ceremony" in flow
        assert flow.count("makes the unusual shape memorable.") == 6
        assert "Reading Mnemonic · すい sounds like a stylish suit." in flow

        scroll = review.query_one("#card-scroll")
        assert scroll.max_scroll_y > 0
        await pilot.press("G")
        assert scroll.scroll_y == scroll.max_scroll_y


@pytest.mark.asyncio
async def test_flow_collapses_metadata_in_order_before_card_content(tmp_path) -> None:
    app, _ = make_app(tmp_path)

    async with app.run_test(size=(90, 20)) as pilot:
        await pilot.press("enter")
        review = app.screen
        assert isinstance(review, ReviewScreen)
        full = rendered_text(review)
        assert "question" in full
        assert "Card 1" in full
        assert "Japanese" in full
        assert "10" in full
        assert "2/1/7" in full

        await pilot.resize_terminal(35, 6)
        no_deck = rendered_text(review)
        assert "question" in no_deck
        assert "Japanese" not in no_deck
        assert "2/1/7" in no_deck
        assert "10" in no_deck
        assert "Card 1" in no_deck

        await pilot.resize_terminal(25, 6)
        no_split = rendered_text(review)
        assert "2/1/7" not in no_split
        assert "10" in no_split
        assert "Card 1" in no_split

        await pilot.resize_terminal(20, 6)
        no_total = rendered_text(review)
        assert "10" not in no_total
        assert "Card 1" in no_total

        await pilot.resize_terminal(16, 6)
        assert rendered_text(review) == "question"


@pytest.mark.asyncio
async def test_long_prompt_and_back_are_complete_and_vim_scrollable_in_tiny_pane(
    tmp_path,
) -> None:
    prompt = "Which storage design remains reliable when every piece of metadata disappears?"
    details = "\n".join(f"detail line {index}" for index in range(30))
    content = RawCardContent(
        CardTemplateIdentity(9, "Architecture", 0, "Long explanation"),
        prompt,
        f"<hr id=answer><h2>Details</h2><pre>{details}</pre>",
    )
    app, _ = make_app(tmp_path, content)

    async with app.run_test(size=(40, 6)) as pilot:
        await pilot.press("enter")
        review = app.screen
        assert isinstance(review, ReviewScreen)
        assert rendered_text(review) == prompt
        assert "detail line" not in rendered_text(review)

        await pilot.press("enter")
        flow = rendered_text(review)
        assert "Details" in flow
        assert "detail line 29" in flow

        scroll = review.query_one("#card-scroll")
        assert scroll.max_scroll_y > 0
        await pilot.press("G")
        assert scroll.scroll_y == scroll.max_scroll_y
        await pilot.press("g")
        assert scroll.scroll_y == 0
        await pilot.press("j")
        assert scroll.scroll_y > 0
        await pilot.press("k")
        assert scroll.scroll_y == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "front_fragment", "back_fragments", "scrolls"),
    [
        (
            RawCardContent(
                CardTemplateIdentity(301, "AWS SAA-C03", 0, "Architecture choice"),
                "Which service decouples producers from consumers?",
                """<hr id=answer>
                <h2>Answer</h2><p>Amazon SQS</p>
                <h2>Why</h2><p>A durable queue buffers messages while consumers
                scale independently.</p>
                <h2>Exam trap</h2><p>SNS fans out notifications; it does not retain
                a queue for a slow consumer.</p>
                """,
            ),
            "Which service decouples",
            ("Amazon SQS", "scale independently", "SNS fans out"),
            True,
        ),
        (
            RawCardContent(
                CardTemplateIdentity(302, "Custom", 0, "Unsupported markup"),
                "<question-shell>What survives unknown markup?</question-shell>",
                """<hr id=answer><answer-shell>
                <mystery-box>All visible answer text survives safely.</mystery-box>
                </answer-shell>""",
            ),
            "What survives unknown markup?",
            ("All visible answer text survives safely.",),
            False,
        ),
    ],
)
async def test_aws_and_unknown_templates_remain_complete_in_tiny_flow(
    tmp_path,
    content,
    front_fragment,
    back_fragments,
    scrolls,
) -> None:
    app, _ = make_app(tmp_path, content)

    async with app.run_test(size=(40, 6)) as pilot:
        await pilot.press("enter")
        review = app.screen
        assert isinstance(review, ReviewScreen)
        assert front_fragment in rendered_text(review)
        assert all(fragment not in rendered_text(review) for fragment in back_fragments)

        await pilot.press("enter")
        flow = rendered_text(review)
        assert all(fragment in flow for fragment in back_fragments)
        assert (review.query_one("#card-scroll").max_scroll_y > 0) is scrolls


@pytest.mark.asyncio
async def test_back_sections_show_fold_hide_and_expand_temporarily(tmp_path) -> None:
    preferences = JsonPreferences(tmp_path / "preferences.json")
    identity = japanese_card().identity
    preferences.set_mode(identity, "back:heading:mnemonic", SectionMode.FOLD)
    preferences.set_mode(identity, "back:heading:examples", SectionMode.HIDE)
    app, _ = make_app(tmp_path, japanese_card(), preferences)

    async with app.run_test(size=(40, 6)) as pilot:
        await pilot.press("enter")
        review = app.screen
        assert isinstance(review, ReviewScreen)
        assert "そう" not in rendered_text(review)
        assert "burial" not in rendered_text(review)
        assert "Flowers" not in rendered_text(review)
        await pilot.press("enter")

        flow = rendered_text(review)
        assert "Reading · そう" in flow
        assert "Meaning · burial; interment" in flow
        assert "› Mnemonic" in flow
        assert "Flowers laid upon a grave." not in flow
        assert "Examples" not in flow
        assert "葬式" not in flow

        await pilot.press("space")
        assert "▾ Mnemonic\nFlowers laid upon a grave." in rendered_text(review)
        assert preferences.mode(
            identity, "back:heading:mnemonic"
        ) is SectionMode.FOLD

        await pilot.press("space")
        assert "Flowers laid upon a grave." not in rendered_text(review)


@pytest.mark.asyncio
async def test_settings_replace_tiny_screen_and_edit_the_current_template(tmp_path) -> None:
    preferences = JsonPreferences(tmp_path / "preferences.json")
    content = japanese_card()
    app, _ = make_app(tmp_path, content, preferences)

    async with app.run_test(size=(40, 6)) as pilot:
        await pilot.press("enter")
        await pilot.press("?")

        settings = app.screen
        assert isinstance(settings, TemplateSettingsScreen)
        assert settings.size.width == 40
        assert settings.size.height == 6
        assert "Japanese / Recognition" in str(
            settings.query_one("#settings-header").render()
        )

        await pilot.press("space")
        assert preferences.mode(
            content.identity, "back:heading:reading"
        ) is SectionMode.FOLD

        await pilot.press("l")
        assert settings.query_one("#settings-sections").display is False
        assert settings.query_one("#settings-controls").display is True
        first_control = settings.query_one("#settings-controls").children[0]
        assert str(first_control.query_one(".control-binding").render()) == "enter"

        await pilot.press("escape")
        review = app.screen
        assert isinstance(review, ReviewScreen)
        await pilot.press("enter")
        assert "› Reading" in rendered_text(review)
        assert "そう" not in rendered_text(review)


@pytest.mark.asyncio
async def test_controls_tab_lists_every_review_action_and_binding_at_40x6(tmp_path) -> None:
    app, _ = make_app(tmp_path)

    async with app.run_test(size=(40, 6)) as pilot:
        await pilot.press("enter", "?", "l")
        settings = app.screen
        assert isinstance(settings, TemplateSettingsScreen)

        controls = settings.query_one("#settings-controls")
        assert controls.display is True
        assert controls.region == (0, 2, 40, 3)
        rows = list(controls.children)
        assert len(rows) == 10
        rendered_rows = [
            (
                str(row.query_one(".control-label").render()),
                str(row.query_one(".control-binding").render()),
            )
            for row in rows
        ]
        assert rendered_rows == [
            ("Reveal / Good", "enter"),
            ("Again", "1"),
            ("Hard", "2"),
            ("Good", "3"),
            ("Easy", "4"),
            ("Undo", "u"),
            ("Bury", "b"),
            ("Suspend", "x"),
            ("Flag", "f"),
            ("Sync", "s"),
        ]
        assert settings.query_one("#settings-footer").region == (0, 5, 40, 1)


@pytest.mark.asyncio
async def test_non_conflicting_control_rebind_saves_and_routes_immediately(tmp_path) -> None:
    preferences = JsonPreferences(tmp_path / "preferences.json")
    app, backend = make_app(tmp_path, preferences=preferences)

    async with app.run_test(size=(40, 6)) as pilot:
        await pilot.press("enter", "?", "l")
        settings = app.screen
        assert isinstance(settings, TemplateSettingsScreen)
        await pilot.press("j", "j", "j", "j", "j", "enter")
        assert str(settings.query_one("#settings-footer").render()).startswith(
            "[?] Undo · press key"
        )

        await pilot.press("z")

        undo_row = settings.query_one("#settings-controls").children[5]
        assert str(undo_row.query_one(".control-binding").render()) == "z"
        assert preferences.review_controls(app.profile).binding(ReviewAction.UNDO) == "z"

        await pilot.press("escape", "enter", "3", "z")
        assert isinstance(app.screen, OperationStatusPill)
        assert backend.undo_calls == 1


@pytest.mark.asyncio
async def test_control_conflict_requires_confirmation_and_leaves_displaced_unbound(
    tmp_path,
) -> None:
    preferences = JsonPreferences(tmp_path / "preferences.json")
    app, backend = make_app(tmp_path, preferences=preferences)

    async with app.run_test(size=(40, 6)) as pilot:
        await pilot.press("enter", "?", "l")
        settings = app.screen
        assert isinstance(settings, TemplateSettingsScreen)
        await pilot.press("j", "j", "j", "j", "j", "enter", "b")

        assert "b = Bury" in str(settings.query_one("#settings-footer").render())
        assert app.review_controls == ReviewControls.defaults()

        await pilot.press("n")
        assert app.review_controls == ReviewControls.defaults()

        await pilot.press("enter", "b", "y")

        assert app.review_controls.binding(ReviewAction.UNDO) == "b"
        assert app.review_controls.binding(ReviewAction.BURY) is None
        rows = settings.query_one("#settings-controls").children
        assert str(rows[5].query_one(".control-binding").render()) == "b"
        assert str(rows[6].query_one(".control-binding").render()) == "unbound"

        await pilot.press("escape", "b")
        assert isinstance(app.screen, OperationStatusPill)
        assert backend.undo_calls == 1
        assert backend.operations == []


@pytest.mark.asyncio
async def test_backspace_restores_selected_control_default_immediately(tmp_path) -> None:
    preferences = JsonPreferences(tmp_path / "preferences.json")
    profile = ProfilePaths(Path("/tmp"), "test", Path("/tmp/collection.anki2"))
    customized = ReviewControls.defaults().with_binding(ReviewAction.UNDO, "z")
    preferences.set_review_controls(profile, customized)
    app, backend = make_app(tmp_path, preferences=preferences)

    async with app.run_test(size=(40, 6)) as pilot:
        await pilot.press("enter", "?", "l", "j", "j", "j", "j", "j")
        settings = app.screen
        assert isinstance(settings, TemplateSettingsScreen)
        await pilot.press("backspace")

        assert app.review_controls.binding(ReviewAction.UNDO) == "u"
        assert preferences.review_controls(profile).binding(ReviewAction.UNDO) == "u"

        await pilot.press("escape", "enter", "3", "z")
        review = app.screen
        assert isinstance(review, ReviewScreen)
        assert backend.undo_calls == 0
        await pilot.press("u")
        assert isinstance(app.screen, OperationStatusPill)
        assert backend.undo_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("fixed_key", ["j", "space"])
async def test_capture_rejects_fixed_navigation_and_escape_cancels_it(
    tmp_path, fixed_key
) -> None:
    app, _ = make_app(tmp_path)

    async with app.run_test(size=(40, 6)) as pilot:
        await pilot.press(
            "enter", "?", "l", "j", "j", "j", "j", "j", "enter", fixed_key
        )
        settings = app.screen
        assert isinstance(settings, TemplateSettingsScreen)
        assert str(settings.query_one("#settings-footer").render()) == (
            f"[fixed] {fixed_key} stays navigation"
        )
        assert app.review_controls == ReviewControls.defaults()

        await pilot.press("escape")
        assert app.screen is settings
        assert str(settings.query_one("#settings-footer").render()) == (
            "j/k · enter bind · bs default · esc"
        )

        await pilot.press("escape")
        assert isinstance(app.screen, ReviewScreen)


@pytest.mark.asyncio
async def test_failed_control_write_preserves_the_active_mapping(
    tmp_path, monkeypatch
) -> None:
    preferences = JsonPreferences(tmp_path / "preferences.json")
    profile = ProfilePaths(Path("/tmp"), "test", Path("/tmp/collection.anki2"))
    original = ReviewControls.defaults().with_binding(ReviewAction.UNDO, "z")
    preferences.set_review_controls(profile, original)
    app, _ = make_app(tmp_path, preferences=preferences)

    async with app.run_test(size=(40, 6)) as pilot:
        await pilot.press("enter", "?", "l", "j", "j", "j", "j", "j", "enter")
        settings = app.screen
        assert isinstance(settings, TemplateSettingsScreen)

        def fail_replace(_source, _destination):
            raise OSError("disk unavailable")

        monkeypatch.setattr(Path, "replace", fail_replace)
        await pilot.press("v")

        assert str(settings.query_one("#settings-footer").render()) == (
            "[err] controls not saved"
        )
        assert app.review_controls == original
        assert preferences.review_controls(profile) == original
        undo_row = settings.query_one("#settings-controls").children[5]
        assert str(undo_row.query_one(".control-binding").render()) == "z"


@pytest.mark.asyncio
async def test_jk_select_folded_rows_and_space_expands_only_the_selected_one(
    tmp_path,
) -> None:
    preferences = JsonPreferences(tmp_path / "preferences.json")
    content = japanese_card()
    preferences.set_mode(
        content.identity, "back:heading:mnemonic", SectionMode.FOLD
    )
    preferences.set_mode(
        content.identity, "back:heading:examples", SectionMode.FOLD
    )
    app, _ = make_app(tmp_path, content, preferences)

    async with app.run_test(size=(40, 6)) as pilot:
        await pilot.press("enter")
        await pilot.press("enter")
        review = app.screen
        assert isinstance(review, ReviewScreen)
        assert "› Mnemonic" in rendered_text(review)
        assert "▸ Examples" in rendered_text(review)

        await pilot.press("j")
        assert "▸ Mnemonic" in rendered_text(review)
        assert "› Examples" in rendered_text(review)

        await pilot.press("space")
        flow = rendered_text(review)
        assert "Flowers laid upon a grave." not in flow
        assert "▾ Examples\n葬式 — funeral" in flow
        assert preferences.mode(
            content.identity, "back:heading:examples"
        ) is SectionMode.FOLD


@pytest.mark.asyncio
@pytest.mark.parametrize(("key", "rating"), (("1", 1), ("2", 2), ("3", 3), ("4", 4)))
async def test_number_keys_rate_only_after_reveal(tmp_path, key, rating) -> None:
    app, backend = make_app(tmp_path)

    async with app.run_test(size=(40, 6)) as pilot:
        await pilot.press("enter")
        await pilot.press(key)
        assert backend.rating is None

        await pilot.press("enter")
        await pilot.press(key)
        assert backend.rating == rating


@pytest.mark.asyncio
async def test_typed_answer_marker_uses_safe_reveal_without_inventing_an_answer(
    tmp_path,
) -> None:
    content = RawCardContent(
        CardTemplateIdentity(55, "Typed", 0, "Type answer"),
        'Capital of France? <input id="typeans" type="text">',
        "<hr id=answer>Paris",
    )
    app, backend = make_app(tmp_path, content)

    async with app.run_test(size=(40, 6)) as pilot:
        await pilot.press("enter")
        review = app.screen
        assert isinstance(review, ReviewScreen)
        assert "[type answer]" not in rendered_text(review)
        assert "Capital of France?" in rendered_text(review)
        assert "Paris" not in rendered_text(review)
        assert len(review.query(Input)) == 0

        await pilot.press("enter")
        assert "Paris" in rendered_text(review)
        await pilot.press("enter")
        assert backend.rating == 3


@pytest.mark.asyncio
async def test_help_is_hidden_until_requested(tmp_path) -> None:
    app, _ = make_app(tmp_path)

    async with app.run_test(size=(70, 20)) as pilot:
        assert isinstance(app.screen, DeckScreen)
        await pilot.press("?")
        assert isinstance(app.screen, HelpScreen)
        await pilot.press("escape")
        assert isinstance(app.screen, DeckScreen)


@pytest.mark.asyncio
async def test_help_is_full_screen_scrollable_and_tiny_pane_safe(tmp_path) -> None:
    app, _ = make_app(tmp_path)

    async with app.run_test(size=(20, 6)) as pilot:
        await pilot.press("?")
        help_screen = app.screen
        assert isinstance(help_screen, HelpScreen)
        assert help_screen.query_one("#help-layout").region.size == help_screen.size
        assert help_screen.query_one("#help-header").region == (0, 0, 20, 1)
        assert help_screen.query_one("#help-scroll").region == (0, 1, 20, 4)
        assert help_screen.query_one(".surface-footer").region == (0, 5, 20, 1)
        assert len(help_screen.query("#help-dialog")) == 0
        scroll = help_screen.query_one("#help-scroll")
        assert scroll.max_scroll_y > 0
        await pilot.press("G")
        assert scroll.scroll_y == scroll.max_scroll_y
        await pilot.press("g")
        assert scroll.scroll_y == 0
        await pilot.press("?")
        assert isinstance(app.screen, DeckScreen)


@pytest.mark.asyncio
async def test_help_lists_the_review_card_operations(tmp_path) -> None:
    app, _ = make_app(tmp_path)

    async with app.run_test(size=(40, 10)) as pilot:
        await pilot.press("?")
        help_text = str(app.screen.query_one("#help-scroll Static").render())
        assert "u        undo" in help_text
        assert "b        bury" in help_text
        assert "x        suspend" in help_text
        assert "f        flag" in help_text


class FailingBackend(FakeBackend):
    def open(self) -> None:
        raise BackendError("Close Anki Desktop before starting repetui.")


@pytest.mark.asyncio
async def test_startup_error_is_a_plain_full_screen_surface(tmp_path) -> None:
    backend = FailingBackend()
    profile = ProfilePaths(Path("/tmp"), "test", Path("/tmp/collection.anki2"))
    app = RepetuiApp(backend, profile, JsonPreferences(tmp_path / "prefs.json"))

    async with app.run_test(size=(20, 6)):
        screen = app.screen
        assert isinstance(screen, ErrorScreen)
        assert screen.query_one("#error-layout").region.size == screen.size
        assert screen.query_one("#error-header").region == (0, 0, 20, 1)
        assert screen.query_one("#error-scroll").region == (0, 1, 20, 4)
        assert screen.query_one(".surface-footer").region == (0, 5, 20, 1)
        assert "unable to start" in str(screen.query_one("#error-header").render())
        assert "Close Anki Desktop" in str(
            screen.query_one("#error-scroll Static").render()
        )
        assert len(screen.query("#error-box")) == 0
        assert len(screen.query("#error-logo")) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("from_review", [False, True])
async def test_sync_opens_the_same_centered_one_line_popup_from_decks_and_review(
    tmp_path,
    from_review,
    monkeypatch,
) -> None:
    started = Event()
    release = Event()

    def fake_sync(profile: ProfilePaths) -> SyncOutcome:
        assert profile.name == "test"
        started.set()
        release.wait(timeout=2)
        return SyncOutcome(SyncStatus.SYNCED)

    app, backend = make_app(tmp_path, syncer=fake_sync)
    notifications = []
    monkeypatch.setattr(
        app,
        "notify",
        lambda *args, **kwargs: notifications.append((args, kwargs)),
    )
    async with app.run_test(size=(40, 6)) as pilot:
        try:
            if from_review:
                await pilot.press("enter")
                assert isinstance(app.screen, ReviewScreen)

            origin = app.screen
            await pilot.press("s")
            assert started.wait(timeout=1)
            await pilot.pause()

            popup = app.screen
            assert isinstance(popup, SyncPopup)
            assert app.screen_stack[-2] is origin
            surface = popup.query_one("#sync-popup")
            assert str(surface.render()) in {
                "[|] syncing...",
                "[/] syncing...",
                "[-] syncing...",
                "[\\] syncing...",
            }
            assert surface.region.height == 1
            assert surface.region.y == 2
            assert surface.region.x >= 1
            assert surface.region.right <= popup.size.width - 1
            assert popup.styles.background.a == 0
            assert backend.is_open is False
            assert notifications == []
        finally:
            release.set()
            await pilot.pause(1.2)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome_status", "visible_message"),
    [
        (SyncStatus.SYNCED, "[ok] synced"),
        (SyncStatus.UP_TO_DATE, "[ok] up to date"),
    ],
)
async def test_sync_completion_replaces_progress_then_dismisses_after_one_second(
    tmp_path,
    outcome_status,
    visible_message,
) -> None:
    app, backend = make_app(
        tmp_path,
        syncer=lambda _profile: SyncOutcome(outcome_status),
    )

    async with app.run_test(size=(40, 6)) as pilot:
        origin = app.screen
        await pilot.press("s")
        await pilot.pause(0.1)

        popup = app.screen
        assert isinstance(popup, SyncPopup)
        assert str(popup.query_one("#sync-popup").render()) == visible_message
        assert backend.is_open is True
        await pilot.pause(0.75)
        assert app.screen is popup
        await pilot.pause(0.3)
        assert app.screen is origin
        assert app.syncing is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "detail", "visible_message", "dismiss_key", "from_review"),
    [
        (
            SyncStatus.OFFLINE,
            "Network connection timed out",
            "[err] offline",
            "enter",
            False,
        ),
        (
            SyncStatus.AUTH_REQUIRED,
            "Open Anki Desktop and complete one sync before using repetui sync.",
            "[err] sign in through Anki",
            "escape",
            False,
        ),
        (
            SyncStatus.COLLECTION_UNAVAILABLE,
            "Collection database is locked",
            "[err] collection unavailable",
            "enter",
            False,
        ),
        (
            SyncStatus.FAILED,
            "secret internal detail",
            "[err] sync failed",
            "escape",
            True,
        ),
    ],
)
async def test_sync_failures_use_concise_persistent_messages_and_dismiss_cleanly(
    tmp_path,
    status,
    detail,
    visible_message,
    dismiss_key,
    from_review,
) -> None:
    app, backend = make_app(
        tmp_path,
        syncer=lambda _profile: SyncOutcome(status, detail),
    )

    async with app.run_test(size=(40, 6)) as pilot:
        if from_review:
            await pilot.press("enter")
            assert isinstance(app.screen, ReviewScreen)
        origin = app.screen
        await pilot.press("s")
        await pilot.pause(0.1)

        popup = app.screen
        assert isinstance(popup, SyncPopup)
        rendered = str(popup.query_one("#sync-popup").render())
        assert rendered == visible_message
        assert detail not in rendered
        await pilot.pause(1.05)
        assert app.screen is popup

        await pilot.press(dismiss_key)
        assert app.screen is origin
        assert app.syncing is False
        assert backend.is_open is True


class ReopenFailingBackend(FakeBackend):
    def __init__(self) -> None:
        super().__init__()
        self.open_count = 0

    def open(self) -> None:
        self.open_count += 1
        if self.open_count > 1:
            raise BackendError("private reopen detail")
        super().open()


class CloseFailingBackend(FakeBackend):
    def __init__(self) -> None:
        super().__init__()
        self.fail_next_close = True

    def close(self) -> None:
        if self.fail_next_close:
            self.fail_next_close = False
            raise BackendError("private close detail")
        super().close()


@pytest.mark.asyncio
async def test_collection_close_failure_becomes_dismissible_without_running_sync(
    tmp_path,
) -> None:
    backend = CloseFailingBackend()
    calls = []
    profile = ProfilePaths(Path("/tmp"), "test", Path("/tmp/collection.anki2"))
    app = RepetuiApp(
        backend,
        profile,
        JsonPreferences(tmp_path / "preferences.json"),
        lambda sync_profile: calls.append(sync_profile),
    )

    async with app.run_test(size=(40, 6)) as pilot:
        origin = app.screen
        await pilot.press("s")
        await pilot.pause(0.1)

        assert isinstance(app.screen, SyncPopup)
        assert str(app.screen.query_one("#sync-popup").render()) == (
            "[err] collection unavailable"
        )
        assert calls == []
        assert backend.is_open is True

        await pilot.press("escape")
        assert app.screen is origin
        assert app.syncing is False


@pytest.mark.asyncio
async def test_collection_reopen_failure_routes_to_fatal_surface_after_dismissal(
    tmp_path,
) -> None:
    backend = ReopenFailingBackend()
    profile = ProfilePaths(Path("/tmp"), "test", Path("/tmp/collection.anki2"))
    app = RepetuiApp(
        backend,
        profile,
        JsonPreferences(tmp_path / "preferences.json"),
        lambda _profile: SyncOutcome(SyncStatus.OFFLINE, "network problem"),
    )

    async with app.run_test(size=(40, 6)) as pilot:
        await pilot.press("s")
        await pilot.pause(0.1)

        popup = app.screen
        assert isinstance(popup, SyncPopup)
        assert str(popup.query_one("#sync-popup").render()) == ("[err] collection unavailable")
        assert "private reopen detail" not in str(popup.query_one("#sync-popup").render())
        assert backend.is_open is False

        await pilot.press("enter")
        assert isinstance(app.screen, ErrorScreen)
        assert "private reopen detail" in str(app.screen.query_one("#error-scroll Static").render())
        assert app.syncing is False


@pytest.mark.asyncio
async def test_sync_popup_blocks_review_navigation_rating_repeat_sync_help_and_quit(
    tmp_path,
) -> None:
    started = Event()
    release = Event()
    calls = 0

    def fake_sync(_profile: ProfilePaths) -> SyncOutcome:
        nonlocal calls
        calls += 1
        started.set()
        release.wait(timeout=2)
        return SyncOutcome(SyncStatus.SYNCED)

    app, backend = make_app(tmp_path, syncer=fake_sync)
    async with app.run_test(size=(40, 6)) as pilot:
        try:
            await pilot.press("enter")
            review = app.screen
            assert isinstance(review, ReviewScreen)
            assert review.revealed is False

            await pilot.press("s")
            assert started.wait(timeout=1)
            await pilot.press(
                "enter",
                "space",
                "1",
                "4",
                "j",
                "k",
                "g",
                "G",
                "escape",
                "s",
                "?",
                "q",
            )

            popup = app.screen
            assert isinstance(popup, SyncPopup)
            assert calls == 1
            assert review.revealed is False
            assert backend.rating is None

            release.set()
            await pilot.pause(0.1)
            assert app.screen is popup
            assert str(popup.query_one("#sync-popup").render()) == "[ok] synced"
            await pilot.press("enter", "3", "escape")
            await pilot.pause(1.0)

            assert app.screen is review
            assert review.revealed is False
            assert backend.rating is None
        finally:
            release.set()


@pytest.mark.asyncio
async def test_sync_spinner_sequence_recenters_and_stays_one_row_in_tiny_panes(
    tmp_path,
    monkeypatch,
) -> None:
    started = Event()
    release = Event()
    monkeypatch.setattr(SyncPopup, "SPINNER_INTERVAL", 60.0)

    def fake_sync(_profile: ProfilePaths) -> SyncOutcome:
        started.set()
        release.wait(timeout=2)
        return SyncOutcome(SyncStatus.SYNCED)

    app, _ = make_app(tmp_path, syncer=fake_sync)
    async with app.run_test(size=(40, 6)) as pilot:
        try:
            await pilot.press("s")
            assert started.wait(timeout=1)
            popup = app.screen
            assert isinstance(popup, SyncPopup)
            surface = popup.query_one("#sync-popup")

            frames = []
            for _ in range(4):
                popup.advance_spinner()
                frames.append(str(surface.render()))
            assert frames == [
                "[/] syncing...",
                "[-] syncing...",
                "[\\] syncing...",
                "[|] syncing...",
            ]

            await pilot.resize_terminal(20, 6)
            assert surface.region.height == 1
            assert surface.region.x == (20 - surface.region.width) // 2

            await pilot.resize_terminal(7, 6)
            assert surface.region == (1, 2, 5, 1)

            await pilot.resize_terminal(6, 6)
            assert surface.region == (1, 2, 4, 1)

            await pilot.resize_terminal(5, 6)
            assert surface.region == (1, 2, 3, 1)

            await pilot.resize_terminal(4, 4)
            assert surface.region.height == 1
            assert surface.region.width == 3
            assert str(surface.render()).startswith("[|]")
        finally:
            release.set()
            await pilot.pause(1.2)


@pytest.mark.asyncio
async def test_app_shutdown_during_sync_does_not_hang_or_reopen_collection(
    tmp_path,
) -> None:
    started = Event()
    release = Event()

    def slow_sync(_profile: ProfilePaths) -> SyncOutcome:
        started.set()
        release.wait(timeout=2)
        return SyncOutcome(SyncStatus.SYNCED)

    app, backend = make_app(tmp_path, syncer=slow_sync)
    try:
        async with app.run_test(size=(40, 6)) as pilot:
            await pilot.press("s")
            assert started.wait(timeout=1)
            assert isinstance(app.screen, SyncPopup)
            assert backend.is_open is False
    finally:
        release.set()

    await asyncio.sleep(0.1)
    assert backend.is_open is False


@pytest.mark.asyncio
async def test_failed_sync_reopens_collection_and_clears_busy_state(tmp_path) -> None:
    def fail_sync(_profile: ProfilePaths) -> SyncOutcome:
        raise RuntimeError("offline")

    app, backend = make_app(tmp_path, syncer=fail_sync)
    async with app.run_test(size=(40, 6)) as pilot:
        await pilot.press("s")
        await pilot.pause(0.1)

        assert backend.is_open is True
        assert isinstance(app.screen, SyncPopup)
        assert str(app.screen.query_one("#sync-popup").render()) == "[err] offline"
        await pilot.press("enter")
        assert app.syncing is False
        assert isinstance(app.screen, DeckScreen)
