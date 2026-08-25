from pathlib import Path

import pytest
from textual.widgets import Input

from repetui.app import (
    DeckScreen,
    HelpScreen,
    RepetuiApp,
    ReviewScreen,
    TemplateSettingsScreen,
)
from repetui.backend import Deck, DueCounts, ReviewCard
from repetui.config import ProfilePaths
from repetui.preferences import SectionMode, SectionPreferences
from repetui.presentation import (
    CardTemplateIdentity,
    RawCardContent,
    SourceField,
    present_card,
)


class FakeBackend:
    def __init__(self, card_content: RawCardContent | None = None) -> None:
        self.is_open = False
        self.rating = None
        self.card_available = True
        self.card_content = card_content

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


def make_app(
    tmp_path: Path | None = None,
    card_content: RawCardContent | None = None,
    preferences: SectionPreferences | None = None,
) -> tuple[RepetuiApp, FakeBackend]:
    backend = FakeBackend(card_content)
    profile = ProfilePaths(Path("/tmp"), "test", Path("/tmp/collection.anki2"))
    store = preferences or SectionPreferences(
        (tmp_path or Path("/tmp")) / "preferences.json"
    )
    return RepetuiApp(backend, profile, store), backend


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
async def test_back_sections_show_fold_hide_and_expand_temporarily(tmp_path) -> None:
    preferences = SectionPreferences(tmp_path / "preferences.json")
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
    preferences = SectionPreferences(tmp_path / "preferences.json")
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
        assert settings.query_one("#settings-keys").display is True
        assert "enter" in str(settings.query_one("#settings-key-text").render())

        await pilot.press("escape")
        review = app.screen
        assert isinstance(review, ReviewScreen)
        await pilot.press("enter")
        assert "› Reading" in rendered_text(review)
        assert "そう" not in rendered_text(review)


@pytest.mark.asyncio
async def test_jk_select_folded_rows_and_space_expands_only_the_selected_one(
    tmp_path,
) -> None:
    preferences = SectionPreferences(tmp_path / "preferences.json")
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
