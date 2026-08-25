"""The complete, deliberately small repetui interface."""

from __future__ import annotations

from typing import Protocol, cast

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import ListItem, ListView, Static

from .backend import AnkiBackend, BackendError, Deck, ReviewCard
from .config import ProfilePaths
from .sync import SyncOutcome, sync_profile

LOGO = """\
       ↻
  ╭────────╮
  │        │╮
  ╰────────╯│
   ╰────────╯  repetui"""


class Refreshable(Protocol):
    def backend_refreshed(self) -> None: ...


class HelpScreen(ModalScreen[None]):
    """One quiet place for shortcuts instead of persistent UI noise."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close", show=False),
        Binding("question_mark", "dismiss", "Close", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield Container(
            Static(
                "[b]repetui[/b]\n\n"
                "[b]Everywhere[/b]\n"
                "  ?       this help\n"
                "  s       sync\n"
                "  q       quit\n\n"
                "[b]Decks[/b]\n"
                "  j / k   move\n"
                "  enter   review\n\n"
                "[b]Review[/b]\n"
                "  enter   reveal / Good\n"
                "  space   reveal\n"
                "  1–4     Again / Hard / Good / Easy\n"
                "  j / k   scroll\n"
                "  g / G   top / bottom\n"
                "  esc     decks\n\n"
                "[dim]Press ? or esc to close.[/dim]",
                markup=True,
            ),
            id="help-dialog",
        )


class ErrorScreen(Screen[None]):
    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        yield Container(
            Static(LOGO, id="error-logo"),
            Static(Text(self.message), id="error-message"),
            Static("[dim]Press q to leave.[/dim]", markup=True, classes="quiet-footer"),
            id="error-box",
        )


class DeckItem(ListItem):
    def __init__(self, deck: Deck) -> None:
        super().__init__()
        self.deck = deck

    def compose(self) -> ComposeResult:
        indent = "  " * self.deck.depth
        counts = self.deck.counts
        yield Static(
            f"{indent}[b]{self.deck.leaf_name}[/b]"
            f"  [dim]due {counts.total}[/dim]  "
            f"[#68a8df]{counts.new}[/]/"
            f"[#dc6b72]{counts.learning}[/]/"
            f"[#79c98b]{counts.review}[/]",
            markup=True,
        )


class DeckScreen(Screen[None]):
    BINDINGS = [
        Binding("j", "down", "Down", show=False),
        Binding("k", "up", "Up", show=False),
        Binding("s", "sync", "Sync", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(LOGO, id="logo"),
            Static("your decks", classes="section-title"),
            ListView(id="decks"),
            Static("[b]?[/b]  help", markup=True, classes="quiet-footer"),
            id="deck-layout",
        )

    def on_mount(self) -> None:
        self.reload()
        self.query_one(ListView).focus()

    def reload(self) -> None:
        view = self.query_one("#decks", ListView)
        view.clear()
        for deck in cast("RepetuiApp", self.app).backend.decks():
            view.append(DeckItem(deck))
        if view.children:
            view.index = 0

    def backend_refreshed(self) -> None:
        self.reload()

    def action_down(self) -> None:
        self.query_one(ListView).action_cursor_down()

    def action_up(self) -> None:
        self.query_one(ListView).action_cursor_up()

    def action_sync(self) -> None:
        cast("RepetuiApp", self.app).action_sync()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item = event.item
        if isinstance(item, DeckItem):
            self.app.push_screen(ReviewScreen(item.deck))


class ReviewScreen(Screen[None]):
    BINDINGS = [
        Binding("escape", "back", "Decks", show=False),
        Binding("enter", "primary", "Reveal/Good", show=False),
        Binding("space", "reveal", "Reveal", show=False),
        Binding("1", "again", "Again", show=False),
        Binding("2", "hard", "Hard", show=False),
        Binding("3", "good", "Good", show=False),
        Binding("4", "easy", "Easy", show=False),
        Binding("j", "scroll_down", "Scroll down", show=False),
        Binding("k", "scroll_up", "Scroll up", show=False),
        Binding("g", "scroll_top", "Top", show=False),
        Binding("G", "scroll_bottom", "Bottom", show=False),
        Binding("s", "sync", "Sync", show=False),
    ]

    def __init__(self, deck: Deck) -> None:
        super().__init__()
        self.deck = deck
        self.card: ReviewCard | None = None
        self.revealed = False

    @property
    def repetui(self) -> RepetuiApp:
        return cast("RepetuiApp", self.app)

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(id="review-header"),
            VerticalScroll(Static(id="card"), id="card-scroll"),
            Horizontal(Static(id="review-actions"), id="action-row"),
            id="review-layout",
        )

    def on_mount(self) -> None:
        self.repetui.backend.begin_review(self.deck.id)
        self.load_next()

    def _busy(self) -> bool:
        if self.repetui.syncing:
            self.notify("Sync is still running.", severity="warning")
            return True
        return False

    def load_next(self) -> None:
        self.card = self.repetui.backend.next_card()
        self.revealed = False
        self._refresh_view()

    def _refresh_view(self) -> None:
        counts = self.repetui.backend.counts()
        self.query_one("#review-header", Static).update(
            f"[b]{self.deck.name}[/b]  [dim]due {counts.total}[/dim]  "
            f"[#68a8df]{counts.new}[/]/"
            f"[#dc6b72]{counts.learning}[/]/"
            f"[#79c98b]{counts.review}[/]"
        )
        content = self.query_one("#card", Static)
        actions = self.query_one("#review-actions", Static)
        if self.card is None:
            content.update(Text("✓  Nothing due. You showed up."))
            actions.update("[dim]esc  decks    s  sync    ?  help[/dim]")
            return

        if self.revealed:
            content.update(
                Text(
                    f"{self.card.presentation.front.text}\n\n────────\n\n"
                    f"{self.card.presentation.back.text}"
                )
            )
            actions.update(
                "[#dc6b72][b]1[/b] again[/]   "
                "[#d7b85a][b]2[/b] hard[/]   "
                "[#79c98b][b]3[/b] good[/]   "
                "[#68a8df][b]4[/b] easy[/]   [dim]?[/dim]"
            )
        else:
            content.update(Text(self.card.presentation.front.text))
            actions.update("[reverse] enter [/reverse] reveal   [dim]?[/dim]")
        self.query_one("#card-scroll", VerticalScroll).scroll_home(animate=False)

    def backend_refreshed(self) -> None:
        self.repetui.backend.begin_review(self.deck.id)
        self.load_next()

    def action_back(self) -> None:
        if not self._busy():
            self.app.pop_screen()

    def action_primary(self) -> None:
        if self.revealed:
            self._rate(3)
        else:
            self.action_reveal()

    def action_reveal(self) -> None:
        if not self._busy() and self.card is not None and not self.revealed:
            self.revealed = True
            self._refresh_view()

    def _rate(self, rating: int) -> None:
        if self._busy() or self.card is None or not self.revealed:
            return
        try:
            self.repetui.backend.answer(rating)
            self.load_next()
        except Exception as exc:
            self.notify(str(exc), severity="error")

    def action_again(self) -> None:
        self._rate(1)

    def action_hard(self) -> None:
        self._rate(2)

    def action_good(self) -> None:
        self._rate(3)

    def action_easy(self) -> None:
        self._rate(4)

    def action_scroll_down(self) -> None:
        self.query_one(VerticalScroll).scroll_down(animate=False)

    def action_scroll_up(self) -> None:
        self.query_one(VerticalScroll).scroll_up(animate=False)

    def action_scroll_top(self) -> None:
        self.query_one(VerticalScroll).scroll_home(animate=False)

    def action_scroll_bottom(self) -> None:
        self.query_one(VerticalScroll).scroll_end(animate=False)

    def action_sync(self) -> None:
        self.repetui.action_sync()


class RepetuiApp(App[None]):
    TITLE = "repetui"
    CSS = """
    Screen {
        background: #111416;
        color: #e7e1d8;
    }

    #deck-layout, #review-layout {
        width: 100%;
        height: 100%;
        max-width: 96;
        align-horizontal: center;
    }

    #logo {
        height: 6;
        color: #9fb8ae;
        text-align: center;
        margin-top: 1;
    }

    .section-title {
        height: 2;
        color: #aaa49b;
        padding: 0 1;
    }

    #decks {
        height: 1fr;
        border: round #394145;
        background: #161a1c;
        margin: 0 1;
    }

    DeckItem {
        height: 1;
        padding: 0 1;
    }

    DeckItem:hover, DeckItem.-highlight {
        background: #293034;
    }

    .quiet-footer {
        height: 1;
        color: #817d76;
        text-align: right;
        padding: 0 2;
    }

    #review-header {
        height: 1;
        background: #1d2225;
        padding: 0 1;
    }

    #card-scroll {
        height: 1fr;
        border: round #394145;
        background: #161a1c;
        margin: 1;
        padding: 1 2;
    }

    #card {
        height: auto;
        min-height: 1;
    }

    #action-row {
        height: 2;
        align-horizontal: center;
    }

    #review-actions {
        width: auto;
        height: 1;
    }

    HelpScreen {
        align: center middle;
        background: #0008;
    }

    #help-dialog {
        width: 58;
        height: auto;
        max-height: 90%;
        border: round #586268;
        background: #171b1d;
        color: #e7e1d8;
        padding: 1 2;
    }

    #error-box {
        width: 70;
        height: auto;
        align: center middle;
        border: round #9d5459;
        padding: 1 2;
    }

    #error-logo {
        height: 6;
        text-align: center;
        color: #9fb8ae;
    }

    #error-message {
        height: auto;
        color: #dc6b72;
        margin: 1 0;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit", show=False, priority=True),
        Binding("question_mark", "help", "Help", show=False, priority=True),
    ]

    def __init__(self, backend: AnkiBackend, profile: ProfilePaths) -> None:
        super().__init__()
        self.backend = backend
        self.profile = profile
        self.syncing = False

    def on_mount(self) -> None:
        try:
            self.backend.open()
            self.push_screen(DeckScreen())
        except BackendError as exc:
            self.push_screen(ErrorScreen(str(exc)))

    def on_unmount(self) -> None:
        self.backend.close()

    def action_help(self) -> None:
        if not isinstance(self.screen, HelpScreen):
            self.push_screen(HelpScreen())

    def action_sync(self) -> None:
        if self.syncing:
            self.notify("Sync is already running.", severity="warning")
            return
        self.syncing = True
        self.notify("Syncing…", timeout=30)
        self._sync_worker()

    @work(thread=True, exclusive=True, group="sync")
    def _sync_worker(self) -> None:
        self.backend.close()
        outcome = sync_profile(self.profile)
        try:
            self.backend.open()
        except Exception as exc:
            outcome = SyncOutcome(False, f"{outcome.message} Could not reopen collection: {exc}")
        self.call_from_thread(self._finish_sync, outcome)

    def _finish_sync(self, outcome: SyncOutcome) -> None:
        self.syncing = False
        self.notify(
            outcome.message,
            severity="information" if outcome.ok else "error",
            timeout=5,
        )
        screen = self.screen
        if hasattr(screen, "backend_refreshed") and self.backend.is_open:
            cast(Refreshable, screen).backend_refreshed()
