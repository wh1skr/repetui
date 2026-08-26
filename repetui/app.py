"""The complete, deliberately small repetui interface."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import Event, Lock, Thread
from typing import Protocol, cast

from rich.cells import cell_len
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen, Screen
from textual.timer import Timer
from textual.widgets import ListItem, ListView, Static

from . import __version__
from .backend import AnkiBackend, BackendError, Deck, ReviewCard
from .config import ProfilePaths
from .deck_tree import VisibleDeckRow, visible_deck_rows
from .flow import SectionState, compose_ratings, compose_review, section_name
from .preferences import JsonPreferences, Preferences, SectionMode
from .presentation import CardTemplateIdentity, PresentationSection
from .sync import SyncOutcome, SyncStatus, failed_sync_outcome, sync_profile


class Refreshable(Protocol):
    def backend_refreshed(self) -> None: ...


@dataclass(frozen=True)
class SyncRunResult:
    outcome: SyncOutcome
    reopen_error: str | None = None


class SyncFinished(Message):
    """Deliver a blocking sync result back to Textual's UI thread."""

    def __init__(self, result: SyncRunResult) -> None:
        super().__init__()
        self.result = result


class HelpScreen(Screen[None]):
    """One quiet place for shortcuts instead of persistent UI noise."""

    BINDINGS = [
        Binding("escape", "back", "Close", show=False),
        Binding("j", "down", "Down", show=False),
        Binding("k", "up", "Up", show=False),
        Binding("g", "top", "Top", show=False),
        Binding("G", "bottom", "Bottom", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static("help · repetui", id="help-header"),
            VerticalScroll(
                Static(
                    "everywhere\n"
                    "  ?        help / template settings\n"
                    "  q        quit\n\n"
                    "decks\n"
                    "  j / k    move\n"
                    "  tab      expand / collapse\n"
                    "  enter    review\n"
                    "  s        sync\n"
                    "  counts   total  new/learning/review\n\n"
                    "review\n"
                    "  enter    reveal / Good\n"
                    "  space    reveal / open selected fold\n"
                    "  1–4      Again / Hard / Good / Easy\n"
                    "  j / k    scroll and select folds\n"
                    "  g / G    top / bottom\n"
                    "  s        sync\n"
                    "  esc      decks\n\n"
                    "template settings\n"
                    "  h / l    sections / keys\n"
                    "  j / k    select / scroll\n"
                    "  space    show → fold → hide\n"
                    "  esc      review"
                ),
                id="help-scroll",
            ),
            Static("j/k scroll · esc return", classes="surface-footer"),
            id="help-layout",
        )

    def action_down(self) -> None:
        self.query_one("#help-scroll", VerticalScroll).scroll_down(animate=False)

    def action_up(self) -> None:
        self.query_one("#help-scroll", VerticalScroll).scroll_up(animate=False)

    def action_top(self) -> None:
        self.query_one("#help-scroll", VerticalScroll).scroll_home(animate=False)

    def action_bottom(self) -> None:
        self.query_one("#help-scroll", VerticalScroll).scroll_end(animate=False)

    def action_back(self) -> None:
        self.app.pop_screen()


class ErrorScreen(Screen[None]):
    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static("repetui · unable to start", id="error-header"),
            VerticalScroll(Static(Text(self.message)), id="error-scroll"),
            Static("q quit · ? help", classes="surface-footer"),
            id="error-layout",
        )


def _deck_identity_candidates(row: VisibleDeckRow) -> tuple[str, ...]:
    marker = "▾ " if row.expanded else "▸ " if row.is_parent else ""
    trail = "> " * row.deck.depth
    leaf_name = row.deck.leaf_name
    candidates = [f"{trail}{marker}{leaf_name}"]
    if marker:
        candidates.append(f"{marker}{leaf_name}")
    if trail:
        candidates.append(f"… {leaf_name}")
    candidates.append(leaf_name)
    return tuple(candidates)


def _deck_identity(row: VisibleDeckRow, width: int) -> Text:
    """Keep the tree state and leaf identity useful as width disappears."""
    leaf_name = row.deck.leaf_name
    for candidate in _deck_identity_candidates(row):
        if cell_len(candidate) <= width:
            return Text(candidate, style="#e7e1d8", no_wrap=True)

    leaf = Text(leaf_name, style="#e7e1d8", no_wrap=True)
    leaf.truncate(max(width, 0), overflow="ellipsis")
    return leaf


def compose_deck_row(row: VisibleDeckRow, width: int) -> Text:
    """Compose one exact-width-aware deck row with predictably shed metadata."""
    deck = row.deck
    counts = deck.counts
    total = Text(str(counts.total), style="bold #d8d3ca", no_wrap=True)
    full_counts = total.copy()
    full_counts.append("  ")
    full_counts.append(str(counts.new), style="#68a8df")
    full_counts.append("/", style="#817d76")
    full_counts.append(str(counts.learning), style="#dc6b72")
    full_counts.append("/", style="#817d76")
    full_counts.append(str(counts.review), style="#79c98b")

    complete_identity_width = cell_len(_deck_identity_candidates(row)[0])
    right = Text()
    if width >= complete_identity_width + 2 + full_counts.cell_len:
        right = full_counts
    elif width >= complete_identity_width + 2 + total.cell_len:
        right = total

    identity_width = max(0, width - right.cell_len - (2 if right else 0))
    identity = _deck_identity(row, identity_width)
    result = identity.copy()
    if right:
        result.append(" " * max(2, width - identity.cell_len - right.cell_len))
        result.append_text(right)
    return result


class DeckItem(ListItem):
    def __init__(self, row: VisibleDeckRow) -> None:
        super().__init__()
        self.row = row

    @property
    def deck(self) -> Deck:
        return self.row.deck

    def compose(self) -> ComposeResult:
        yield Static(classes="deck-row")

    def on_mount(self) -> None:
        self._refresh_counts(self.size.width)

    def on_resize(self) -> None:
        self._refresh_counts(self.size.width)

    def _refresh_counts(self, width: int) -> None:
        self.query_one(".deck-row", Static).update(compose_deck_row(self.row, width))

    def flash_selection(self) -> None:
        """Give a leaf brief row-only feedback without adding interface chrome."""
        self.add_class("-leaf-feedback")
        self.set_timer(0.15, lambda: self.remove_class("-leaf-feedback"))


class DeckScreen(Screen[None]):
    BINDINGS = [
        Binding("j", "down", "Down", show=False),
        Binding("k", "up", "Up", show=False),
        Binding("tab", "toggle_deck", "Expand/collapse", show=False),
        Binding("s", "sync", "Sync", show=False),
    ]

    @property
    def repetui(self) -> RepetuiApp:
        return cast("RepetuiApp", self.app)

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(f"decks · repetui {__version__}", id="deck-header"),
            ListView(id="decks"),
            id="deck-layout",
        )

    def on_mount(self) -> None:
        self.reload()
        self.query_one(ListView).focus()

    def reload(self, selected_deck_id: int | None = None) -> None:
        view = self.query_one("#decks", ListView)
        old_index = view.index or 0
        if selected_deck_id is None and view.index is not None:
            children = list(view.children)
            if 0 <= view.index < len(children) and isinstance(
                children[view.index], DeckItem
            ):
                selected_deck_id = children[view.index].deck.id

        rows = visible_deck_rows(
            self.repetui.backend.decks(),
            self.repetui.preferences.expanded_deck_ids(self.repetui.profile),
        )
        view.clear()
        for row in rows:
            view.append(DeckItem(row))
        if rows:
            selected_index = next(
                (
                    index
                    for index, row in enumerate(rows)
                    if row.deck.id == selected_deck_id
                ),
                min(old_index, len(rows) - 1),
            )
            view.index = selected_index

    def backend_refreshed(self) -> None:
        self.reload()

    def action_down(self) -> None:
        self.query_one(ListView).action_cursor_down()

    def action_up(self) -> None:
        self.query_one(ListView).action_cursor_up()

    def action_toggle_deck(self) -> None:
        view = self.query_one(ListView)
        if view.index is None or not (0 <= view.index < len(view.children)):
            return
        item = view.children[view.index]
        if not isinstance(item, DeckItem):
            return
        if not item.row.is_parent:
            item.flash_selection()
            return
        self.repetui.preferences.set_deck_expanded(
            self.repetui.profile,
            item.deck.id,
            expanded=not item.row.expanded,
        )
        self.reload(selected_deck_id=item.deck.id)

    def action_sync(self) -> None:
        cast("RepetuiApp", self.app).action_sync()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item = event.item
        if isinstance(item, DeckItem):
            self.app.push_screen(ReviewScreen(item.deck))


class SectionSettingItem(ListItem):
    """One keyboard-editable presentation section."""

    def __init__(self, section: PresentationSection) -> None:
        super().__init__()
        self.section = section

    def compose(self) -> ComposeResult:
        yield Static(classes="setting-label")
        yield Static(classes="setting-mode")

    def refresh_mode(
        self, preferences: Preferences, identity: CardTemplateIdentity
    ) -> None:
        mode = preferences.mode(identity, self.section.id)
        label = section_name(self.section)
        colour = {
            SectionMode.SHOW: "#79c98b",
            SectionMode.FOLD: "#d7b85a",
            SectionMode.HIDE: "#dc6b72",
        }[mode]
        self.query_one(".setting-label", Static).update(
            Text(label, style="#d9d5ce", overflow="ellipsis", no_wrap=True)
        )
        self.query_one(".setting-mode", Static).update(Text(mode.value, style=colour))


class TemplateSettingsScreen(Screen[None]):
    """A full-screen, tiny-pane-safe surface for one card template."""

    BINDINGS = [
        Binding("escape", "back", "Back", show=False),
        Binding("j", "down", "Down", show=False),
        Binding("k", "up", "Up", show=False),
        Binding("g", "top", "Top", show=False),
        Binding("G", "bottom", "Bottom", show=False),
        Binding("space", "cycle", "Change", show=False),
        Binding("enter", "cycle", "Change", show=False),
        Binding("h", "sections", "Sections", show=False),
        Binding("l", "keys", "Keys", show=False),
        Binding("tab", "toggle_tab", "Next tab", show=False),
    ]

    def __init__(self, review: ReviewScreen) -> None:
        super().__init__()
        self.review = review
        assert review.card is not None
        self.card = review.card
        self.tab = "sections"

    @property
    def repetui(self) -> RepetuiApp:
        return cast("RepetuiApp", self.app)

    def compose(self) -> ComposeResult:
        identity = self.card.presentation.identity
        yield Vertical(
            Static(
                Text(
                    f"settings · {identity.note_type_name} / {identity.template_name}",
                    overflow="ellipsis",
                    no_wrap=True,
                ),
                id="settings-header",
            ),
            Static(id="settings-tabs"),
            ListView(
                *(SectionSettingItem(section) for section in self.card.presentation.back.sections),
                id="settings-sections",
            ),
            VerticalScroll(
                Static(
                    "review\n"
                    "  enter    reveal / Good\n"
                    "  1–4      Again / Hard / Good / Easy\n"
                    "  space    reveal / open selected fold\n"
                    "  j / k    scroll and select folds\n"
                    "  g / G    top / bottom\n"
                    "  s        sync\n"
                    "  esc      decks\n\n"
                    "settings\n"
                    "  h / l    sections / keys\n"
                    "  j / k    select section\n"
                    "  space    show → fold → hide\n"
                    "  esc      return",
                    id="settings-key-text",
                ),
                id="settings-keys",
            ),
            Static(
                "j/k move · space mode · h/l tabs · esc",
                id="settings-footer",
                classes="surface-footer",
            ),
            id="settings-layout",
        )

    def on_mount(self) -> None:
        for item in self.query(SectionSettingItem):
            item.refresh_mode(self.repetui.preferences, self.card.presentation.identity)
        sections = self.query_one("#settings-sections", ListView)
        if sections.children:
            sections.index = 0
        self._show_tab("sections")

    def _show_tab(self, tab: str) -> None:
        self.tab = tab
        sections = self.query_one("#settings-sections", ListView)
        keys = self.query_one("#settings-keys", VerticalScroll)
        sections.display = tab == "sections"
        keys.display = tab == "keys"
        self.query_one("#settings-tabs", Static).update(
            "[reverse] sections [/reverse]  keys"
            if tab == "sections"
            else "sections  [reverse] keys [/reverse]"
        )
        (sections if tab == "sections" else keys).focus()

    def action_sections(self) -> None:
        self._show_tab("sections")

    def action_keys(self) -> None:
        self._show_tab("keys")

    def action_toggle_tab(self) -> None:
        self._show_tab("keys" if self.tab == "sections" else "sections")

    def action_down(self) -> None:
        if self.tab == "sections":
            self.query_one("#settings-sections", ListView).action_cursor_down()
        else:
            self.query_one("#settings-keys", VerticalScroll).scroll_down(animate=False)

    def action_up(self) -> None:
        if self.tab == "sections":
            self.query_one("#settings-sections", ListView).action_cursor_up()
        else:
            self.query_one("#settings-keys", VerticalScroll).scroll_up(animate=False)

    def action_top(self) -> None:
        if self.tab == "sections":
            view = self.query_one("#settings-sections", ListView)
            if view.children:
                view.index = 0
        else:
            self.query_one("#settings-keys", VerticalScroll).scroll_home(animate=False)

    def action_bottom(self) -> None:
        if self.tab == "sections":
            view = self.query_one("#settings-sections", ListView)
            if view.children:
                view.index = len(view.children) - 1
        else:
            self.query_one("#settings-keys", VerticalScroll).scroll_end(animate=False)

    def action_cycle(self) -> None:
        if self.tab != "sections":
            return
        view = self.query_one("#settings-sections", ListView)
        if view.index is None or not (0 <= view.index < len(view.children)):
            return
        item = view.children[view.index]
        if not isinstance(item, SectionSettingItem):
            return
        identity = self.card.presentation.identity
        mode = self.repetui.preferences.mode(identity, item.section.id)
        self.repetui.preferences.set_mode(identity, item.section.id, mode.next)
        item.refresh_mode(self.repetui.preferences, identity)

    def action_back(self) -> None:
        self.app.pop_screen()
        self.review.preferences_changed()


class ReviewScreen(Screen[None]):
    BINDINGS = [
        Binding("escape", "back", "Decks", show=False),
        Binding("enter", "primary", "Reveal/Good", show=False),
        Binding("space", "toggle_fold", "Reveal/expand", show=False),
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
        self.expanded_sections: set[str] = set()
        self.selected_folded = 0

    @property
    def repetui(self) -> RepetuiApp:
        return cast("RepetuiApp", self.app)

    def compose(self) -> ComposeResult:
        yield Vertical(
            VerticalScroll(Static(id="card"), id="card-scroll"),
            Static(id="review-actions"),
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
        self.expanded_sections.clear()
        self.selected_folded = 0
        self._refresh_view()

    def _folded_sections(self) -> tuple[PresentationSection, ...]:
        if self.card is None:
            return ()
        identity = self.card.presentation.identity
        return tuple(
            section
            for section in self.card.presentation.back.sections
            if self.repetui.preferences.mode(identity, section.id) is SectionMode.FOLD
        )

    def _section_states(self) -> tuple[SectionState, ...]:
        assert self.card is not None
        identity = self.card.presentation.identity
        folded = self._folded_sections()
        folded_ids = [section.id for section in folded]
        if folded_ids:
            self.selected_folded %= len(folded_ids)
        else:
            self.selected_folded = 0

        states: list[SectionState] = []
        for section in self.card.presentation.back.sections:
            mode = self.repetui.preferences.mode(identity, section.id)
            states.append(
                SectionState(
                    section=section,
                    mode=mode,
                    expanded=section.id in self.expanded_sections,
                    selected=(
                        mode is SectionMode.FOLD
                        and folded_ids.index(section.id) == self.selected_folded
                    ),
                )
            )
        return tuple(states)

    def _refresh_view(self, *, reset_scroll: bool = True) -> None:
        counts = self.repetui.backend.counts()
        content = self.query_one("#card", Static)
        actions = self.query_one("#review-actions", Static)
        if self.card is None:
            complete = Text("done · ", style="#79c98b")
            complete.append(self.deck.leaf_name, style="bold #eee9e0")
            complete.append("\nNothing due. You showed up.", style="#aaa49b")
            content.update(complete)
            actions.display = False
            return

        flow = compose_review(
            self.card.presentation,
            self.deck.name,
            counts,
            self.size.width,
            revealed=self.revealed,
            sections=self._section_states() if self.revealed else (),
        )
        if self.revealed:
            actions.update(compose_ratings(self.size.width))
            actions.display = True
        else:
            actions.display = False
        content.update(flow)
        if reset_scroll:
            self.query_one("#card-scroll", VerticalScroll).scroll_home(animate=False)

    def on_resize(self) -> None:
        if self.is_mounted:
            self._refresh_view(reset_scroll=False)

    def backend_refreshed(self) -> None:
        self.repetui.backend.begin_review(self.deck.id)
        self.load_next()

    def preferences_changed(self) -> None:
        """Apply saved choices after the settings screen returns."""
        self.expanded_sections.clear()
        self.selected_folded = 0
        self._refresh_view(reset_scroll=False)

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

    def action_toggle_fold(self) -> None:
        if not self.revealed:
            self.action_reveal()
            return
        folded = self._folded_sections()
        if not folded:
            return
        section = folded[self.selected_folded % len(folded)]
        if section.id in self.expanded_sections:
            self.expanded_sections.remove(section.id)
        else:
            self.expanded_sections.add(section.id)
        self._refresh_view(reset_scroll=False)

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
        folded = self._folded_sections()
        if self.revealed and folded:
            self.selected_folded = (self.selected_folded + 1) % len(folded)
            self._refresh_view(reset_scroll=False)
        self.query_one(VerticalScroll).scroll_down(animate=False)

    def action_scroll_up(self) -> None:
        folded = self._folded_sections()
        if self.revealed and folded:
            self.selected_folded = (self.selected_folded - 1) % len(folded)
            self._refresh_view(reset_scroll=False)
        self.query_one(VerticalScroll).scroll_up(animate=False)

    def action_scroll_top(self) -> None:
        self.query_one(VerticalScroll).scroll_home(animate=False)

    def action_scroll_bottom(self) -> None:
        self.query_one(VerticalScroll).scroll_end(animate=False)

    def action_sync(self) -> None:
        self.repetui.action_sync()


class SyncPopup(ModalScreen[bool]):
    """One modal, terminal-native sync message over the originating screen."""

    SPINNER_FRAMES = ("|", "/", "-", "\\")
    SPINNER_INTERVAL = 0.12

    BINDINGS = [
        Binding("q", "block", show=False, priority=True),
        Binding("question_mark", "block", show=False, priority=True),
        Binding("s", "block", show=False),
        Binding("escape", "dismiss_failure", show=False),
        Binding("enter", "dismiss_failure", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._frame_index = 0
        self._spinner_timer: Timer | None = None
        self._dismiss_timer: Timer | None = None
        self._failure_dismissible = False
        self._fatal = False
        self._message = "[|] syncing..."

    def compose(self) -> ComposeResult:
        yield Static(Text(self._message, no_wrap=True), id="sync-popup")

    def on_mount(self) -> None:
        self._spinner_timer = self.set_interval(self.SPINNER_INTERVAL, self.advance_spinner)
        self._fit_surface()

    def on_resize(self) -> None:
        if self.is_mounted:
            self._fit_surface()

    def on_unmount(self) -> None:
        self._stop_timers()

    def advance_spinner(self) -> None:
        """Advance the visible ASCII sync frame in its fixed sequence."""
        self._frame_index = (self._frame_index + 1) % len(self.SPINNER_FRAMES)
        frame = self.SPINNER_FRAMES[self._frame_index]
        self._show_message(f"[{frame}] syncing...")

    def finish(self, result: SyncRunResult) -> None:
        self._stop_spinner()
        outcome = result.outcome
        if outcome.ok:
            message = {
                SyncStatus.SYNCED: "[ok] synced",
                SyncStatus.UP_TO_DATE: "[ok] up to date",
            }[outcome.status]
            self._show_message(message)
            self.query_one("#sync-popup").add_class("-success")
            self._dismiss_timer = self.set_timer(1.0, self._dismiss_success)
        else:
            self._failure_dismissible = True
            self._fatal = result.reopen_error is not None
            message = (
                "[err] collection unavailable"
                if self._fatal
                else {
                    SyncStatus.OFFLINE: "[err] offline",
                    SyncStatus.AUTH_REQUIRED: "[err] sign in through Anki",
                    SyncStatus.COLLECTION_UNAVAILABLE: "[err] collection unavailable",
                    SyncStatus.FAILED: "[err] sync failed",
                }[outcome.status]
            )
            self._show_message(message)
            self.query_one("#sync-popup").add_class("-error")

    def _show_message(self, message: str) -> None:
        self._message = message
        self.query_one("#sync-popup", Static).update(Text(message, no_wrap=True))
        self._fit_surface()

    def _fit_surface(self) -> None:
        """Keep padding and surroundings until the state marker needs the cells."""
        surface = self.query_one("#sync-popup", Static)
        pane_width = max(self.size.width, 1)
        if pane_width >= 7:
            horizontal_padding = 1
            width = min(cell_len(self._message) + 2, pane_width - 2)
        elif pane_width >= 5:
            horizontal_padding = 0
            width = min(cell_len(self._message), pane_width - 2)
        else:
            horizontal_padding = 0
            width = min(cell_len(self._message), pane_width, 3)
        surface.styles.padding = (0, horizontal_padding)
        surface.styles.width = max(width, 1)

    def _stop_spinner(self) -> None:
        if self._spinner_timer is not None:
            self._spinner_timer.stop()
            self._spinner_timer = None

    def _stop_timers(self) -> None:
        self._stop_spinner()
        if self._dismiss_timer is not None:
            self._dismiss_timer.stop()
            self._dismiss_timer = None

    def _dismiss_success(self) -> None:
        self.dismiss(False)

    def action_block(self) -> None:
        """Consume keys while sync owns the collection and interaction."""

    def action_dismiss_failure(self) -> None:
        if self._failure_dismissible:
            self._stop_timers()
            self.dismiss(self._fatal)


class RepetuiApp(App[None]):
    TITLE = f"repetui {__version__}"
    CSS = """
    Screen {
        background: #111416;
        color: #e7e1d8;
    }

    #deck-layout {
        width: 100%;
        height: 100%;
    }

    #review-layout {
        width: 100%;
        height: 100%;
    }

    #deck-header, #help-header, #error-header {
        height: 1;
        color: #eee9e0;
    }

    #decks {
        height: 1fr;
        background: #111416;
    }

    DeckItem {
        height: 1;
    }

    DeckItem:hover, DeckItem.-highlight {
        background: #293034;
    }

    DeckItem.-leaf-feedback {
        background: #465158;
    }

    .deck-row {
        width: 100%;
        height: 1;
        overflow: hidden;
    }

    #card-scroll {
        height: 1fr;
        background: #111416;
        scrollbar-size-vertical: 1;
    }

    #card {
        height: auto;
        min-height: 1;
    }

    #review-actions {
        width: 100%;
        height: 1;
        text-align: center;
    }

    #settings-layout {
        width: 100%;
        height: 100%;
        background: #111416;
    }

    #settings-header {
        height: 1;
        color: #eee9e0;
    }

    #settings-tabs {
        height: 1;
        color: #aaa49b;
    }

    #settings-sections, #settings-keys {
        height: 1fr;
        background: #111416;
        scrollbar-size-vertical: 1;
    }

    SectionSettingItem {
        height: 1;
        layout: horizontal;
    }

    SectionSettingItem.-highlight {
        background: #293034;
    }

    .setting-label {
        width: 1fr;
        height: 1;
    }

    .setting-mode {
        width: 5;
        height: 1;
        text-align: right;
    }

    #settings-key-text {
        height: auto;
        color: #d9d5ce;
    }

    .surface-footer {
        height: 1;
        color: #817d76;
        overflow: hidden;
    }

    #help-layout, #error-layout {
        width: 100%;
        height: 100%;
        background: #111416;
    }

    #help-scroll, #error-scroll {
        height: 1fr;
        scrollbar-size-vertical: 1;
    }

    #error-header, #error-scroll {
        color: #dc6b72;
    }

    SyncPopup {
        align: center middle;
        overflow: hidden;
        background: transparent;
    }

    #sync-popup {
        width: auto;
        max-width: 100%;
        height: 1;
        padding: 0 1;
        overflow: hidden;
        text-wrap: nowrap;
        text-overflow: ellipsis;
        background: #293034;
        color: #e7e1d8;
    }

    #sync-popup.-success {
        color: #79c98b;
    }

    #sync-popup.-error {
        color: #dc6b72;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit", show=False, priority=True),
        Binding("question_mark", "help", "Help", show=False, priority=True),
    ]

    def __init__(
        self,
        backend: AnkiBackend,
        profile: ProfilePaths,
        preferences: Preferences | None = None,
        syncer: Callable[[ProfilePaths], SyncOutcome] = sync_profile,
    ) -> None:
        super().__init__()
        self.backend = backend
        self.profile = profile
        self.preferences = preferences if preferences is not None else JsonPreferences()
        self.syncer = syncer
        self.syncing = False
        self._sync_origin: Screen[None] | None = None
        self._sync_popup: SyncPopup | None = None
        self._sync_thread: Thread | None = None
        self._sync_fatal_error: str | None = None
        self._shutdown_requested = Event()
        self._backend_lock = Lock()

    def on_mount(self) -> None:
        try:
            self.backend.open()
            self.push_screen(DeckScreen())
        except BackendError as exc:
            self.push_screen(ErrorScreen(str(exc)))

    def on_unmount(self) -> None:
        self._shutdown_requested.set()
        with self._backend_lock:
            self.backend.close()

    def action_help(self) -> None:
        if self.syncing:
            return
        screen = self.screen
        if isinstance(screen, (TemplateSettingsScreen, HelpScreen)):
            screen.action_back()
        elif isinstance(screen, ReviewScreen) and screen.card is not None:
            self.push_screen(TemplateSettingsScreen(screen))
        else:
            self.push_screen(HelpScreen())

    def action_quit(self) -> None:
        if not self.syncing:
            self.exit()

    def action_sync(self) -> None:
        if self.syncing:
            return
        self.syncing = True
        self._sync_origin = self.screen
        self._sync_popup = SyncPopup()
        self.push_screen(self._sync_popup, self._sync_popup_closed)
        self.call_after_refresh(self._start_sync_thread)

    def _start_sync_thread(self) -> None:
        self._sync_thread = Thread(
            target=self._sync_in_thread,
            name="repetui-sync",
            daemon=True,
        )
        self._sync_thread.start()

    def _sync_in_thread(self) -> None:
        self.post_message(SyncFinished(self._run_sync()))

    def on_sync_finished(self, message: SyncFinished) -> None:
        self._finish_sync(message.result)

    def _run_sync(self) -> SyncRunResult:
        """Run the blocking close/sync/reopen sequence without UI mutation."""
        close_error = None
        with self._backend_lock:
            if self._shutdown_requested.is_set():
                return SyncRunResult(SyncOutcome(SyncStatus.FAILED, "Sync cancelled."))
            try:
                self.backend.close()
            except Exception as exc:
                close_error = exc
        if close_error is not None:
            outcome = SyncOutcome(SyncStatus.COLLECTION_UNAVAILABLE, str(close_error))
        else:
            try:
                outcome = self.syncer(self.profile)
            except Exception as exc:
                outcome = failed_sync_outcome(exc)
        reopen_error = None
        with self._backend_lock:
            if not self._shutdown_requested.is_set():
                try:
                    self.backend.open()
                except Exception as exc:
                    reopen_error = str(exc)
        return SyncRunResult(outcome, reopen_error)

    def _finish_sync(self, result: SyncRunResult) -> None:
        origin = self._sync_origin
        if origin is not None and hasattr(origin, "backend_refreshed") and self.backend.is_open:
            cast(Refreshable, origin).backend_refreshed()
        self._sync_fatal_error = result.reopen_error
        if self._sync_popup is not None:
            self._sync_popup.finish(result)

    def _sync_popup_closed(self, fatal: bool | None) -> None:
        fatal_error = self._sync_fatal_error
        self.syncing = False
        self._sync_popup = None
        self._sync_origin = None
        self._sync_fatal_error = None
        if fatal and fatal_error is not None:
            self.push_screen(ErrorScreen(f"Could not reopen the Anki collection: {fatal_error}"))
