"""The complete, deliberately small repetui interface."""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from threading import Event, Lock, Thread
from typing import Protocol, cast

from rich.cells import cell_len
from rich.text import Text
from textual import events
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
from .controls import (
    DEFAULT_REVIEW_BINDINGS,
    BindingConflict,
    ReviewAction,
    ReviewControls,
)
from .deck_tree import VisibleDeckRow, visible_deck_rows
from .flow import (
    SectionState,
    compose_rating_feedback,
    compose_ratings,
    compose_review,
    section_name,
)
from .preferences import AnswerLayout, JsonPreferences, Preferences, SectionMode
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


_HELP_TEXT = (
    "everywhere\n"
    "  ?        settings\n"
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
    "  u        undo\n"
    "  b        bury\n"
    "  x        suspend\n"
    "  f        flag (then 0–7)\n"
    "  j / k    scroll and select folds\n"
    "  g / G    top / bottom\n"
    "  s        sync\n"
    "  esc      decks\n\n"
    "settings\n"
    "  h / l    previous / next tab\n"
    "  tab      next tab\n"
    "  j / k    select / scroll\n"
    "  space    show → fold → hide\n"
    "  esc      return"
)


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


class AnswerLayoutSettingItem(ListItem):
    """The current template's compact or left-aligned answer flow."""

    def compose(self) -> ComposeResult:
        yield Static("answer layout", classes="setting-label")
        yield Static(classes="setting-mode")

    def refresh_layout(
        self, preferences: Preferences, identity: CardTemplateIdentity
    ) -> None:
        layout = preferences.answer_layout(identity)
        colour = "#79c98b" if layout is AnswerLayout.STACKED else "#aaa49b"
        self.query_one(".setting-mode", Static).update(
            Text(layout.value, style=colour, no_wrap=True)
        )


class ControlSettingItem(ListItem):
    """One keyboard-editable review action and its current binding."""

    def __init__(self, action: ReviewAction) -> None:
        super().__init__()
        self.action = action

    def compose(self) -> ComposeResult:
        yield Static(self.action.label, classes="control-label")
        yield Static(classes="control-binding")

    def refresh_binding(self, controls: ReviewControls) -> None:
        key = controls.binding(self.action)
        style = "#d9d5ce" if key is not None else "#dc6b72"
        self.query_one(".control-binding", Static).update(
            Text(key or "unbound", style=style, no_wrap=True)
        )


@dataclass(frozen=True)
class PendingControlBinding:
    action: ReviewAction
    key: str
    conflict: ReviewAction


class SettingsScreen(Screen[None]):
    """One tiny-pane-safe home for help, controls, and card sections."""

    TABS = ("help", "controls", "sections")

    BINDINGS = [
        Binding("escape", "back", "Back", show=False),
        Binding("j", "down", "Down", show=False),
        Binding("k", "up", "Up", show=False),
        Binding("g", "top", "Top", show=False),
        Binding("G", "bottom", "Bottom", show=False),
        Binding("space", "cycle", "Change", show=False),
        Binding("enter", "cycle", "Change", show=False),
        Binding("h", "previous_tab", "Previous tab", show=False),
        Binding("l", "next_tab", "Next tab", show=False),
        Binding("tab", "next_tab", "Next tab", show=False),
    ]

    def __init__(
        self,
        review: ReviewScreen | None = None,
        *,
        initial_tab: str = "help",
    ) -> None:
        super().__init__()
        self.review = review
        self.card = review.card if review is not None else None
        if initial_tab not in self.TABS:
            raise ValueError(f"Unknown settings tab: {initial_tab}")
        if initial_tab == "sections" and self.card is None:
            initial_tab = "help"
        self.initial_tab = initial_tab
        self.tab = initial_tab
        self.capturing: ReviewAction | None = None
        self.pending_binding: PendingControlBinding | None = None

    @property
    def repetui(self) -> RepetuiApp:
        return cast("RepetuiApp", self.app)

    def compose(self) -> ComposeResult:
        sections = self.card.presentation.back.sections if self.card is not None else ()
        yield Vertical(
            Static("settings", id="settings-header"),
            Static(id="settings-tabs"),
            VerticalScroll(
                Static(_HELP_TEXT),
                id="settings-help",
            ),
            ListView(
                *(AnswerLayoutSettingItem(),) if self.card is not None else (),
                *(SectionSettingItem(section) for section in sections),
                id="settings-sections",
            ),
            Static(
                "review a card to configure its sections",
                id="settings-sections-empty",
            ),
            ListView(
                *(ControlSettingItem(action) for action in ReviewAction),
                id="settings-controls",
            ),
            Static(
                "j/k · enter bind · bs default · esc",
                id="settings-footer",
                classes="surface-footer",
            ),
            id="settings-layout",
        )

    def on_mount(self) -> None:
        if self.card is not None:
            self.query_one(AnswerLayoutSettingItem).refresh_layout(
                self.repetui.preferences, self.card.presentation.identity
            )
            for item in self.query(SectionSettingItem):
                item.refresh_mode(
                    self.repetui.preferences, self.card.presentation.identity
                )
        for item in self.query(ControlSettingItem):
            item.refresh_binding(self.repetui.review_controls)
        sections = self.query_one("#settings-sections", ListView)
        if sections.children:
            sections.index = 0
        self._show_tab(self.initial_tab)

    def _show_tab(self, tab: str) -> None:
        self.tab = tab
        help_scroll = self.query_one("#settings-help", VerticalScroll)
        sections = self.query_one("#settings-sections", ListView)
        sections_empty = self.query_one("#settings-sections-empty", Static)
        controls = self.query_one("#settings-controls", ListView)
        help_scroll.display = tab == "help"
        sections.display = tab == "sections" and self.card is not None
        sections_empty.display = tab == "sections" and self.card is None
        controls.display = tab == "controls"
        self.query_one("#settings-tabs", Static).update(
            "  ".join(
                f"[reverse] {name} [/reverse]" if name == tab else name
                for name in self.TABS
            )
        )
        if tab == "help":
            help_scroll.focus()
        elif tab == "controls":
            if controls.children and controls.index is None:
                controls.index = 0
            controls.focus()
        elif self.card is not None:
            if sections.children and sections.index is None:
                sections.index = 0
            sections.focus()
        self._show_default_footer()

    def action_previous_tab(self) -> None:
        index = self.TABS.index(self.tab)
        self._show_tab(self.TABS[(index - 1) % len(self.TABS)])

    def action_next_tab(self) -> None:
        index = self.TABS.index(self.tab)
        self._show_tab(self.TABS[(index + 1) % len(self.TABS)])

    def action_down(self) -> None:
        if self.tab == "help":
            self.query_one("#settings-help", VerticalScroll).scroll_down(animate=False)
        elif self.tab == "sections" and self.card is not None:
            self.query_one("#settings-sections", ListView).action_cursor_down()
        elif self.tab == "controls":
            self.query_one("#settings-controls", ListView).action_cursor_down()

    def action_up(self) -> None:
        if self.tab == "help":
            self.query_one("#settings-help", VerticalScroll).scroll_up(animate=False)
        elif self.tab == "sections" and self.card is not None:
            self.query_one("#settings-sections", ListView).action_cursor_up()
        elif self.tab == "controls":
            self.query_one("#settings-controls", ListView).action_cursor_up()

    def action_top(self) -> None:
        if self.tab == "help":
            self.query_one("#settings-help", VerticalScroll).scroll_home(animate=False)
            return
        if self.tab == "sections" and self.card is not None:
            view = self.query_one("#settings-sections", ListView)
        elif self.tab == "controls":
            view = self.query_one("#settings-controls", ListView)
        else:
            return
        if view.children:
            view.index = 0

    def action_bottom(self) -> None:
        if self.tab == "help":
            self.query_one("#settings-help", VerticalScroll).scroll_end(animate=False)
            return
        if self.tab == "sections" and self.card is not None:
            view = self.query_one("#settings-sections", ListView)
        elif self.tab == "controls":
            view = self.query_one("#settings-controls", ListView)
        else:
            return
        if view.children:
            view.index = len(view.children) - 1

    def action_cycle(self) -> None:
        if self.tab == "controls":
            action = self._selected_control_action()
            if action is not None:
                self.capturing = action
                self._show_footer(f"[?] {action.label} · press key · esc cancel")
            return
        if self.tab != "sections" or self.card is None:
            return
        view = self.query_one("#settings-sections", ListView)
        if view.index is None or not (0 <= view.index < len(view.children)):
            return
        item = view.children[view.index]
        identity = self.card.presentation.identity
        if isinstance(item, AnswerLayoutSettingItem):
            layout = self.repetui.preferences.answer_layout(identity)
            self.repetui.preferences.set_answer_layout(identity, layout.next)
            item.refresh_layout(self.repetui.preferences, identity)
            return
        if not isinstance(item, SectionSettingItem):
            return
        mode = self.repetui.preferences.mode(identity, item.section.id)
        self.repetui.preferences.set_mode(identity, item.section.id, mode.next)
        item.refresh_mode(self.repetui.preferences, identity)

    def _selected_control_action(self) -> ReviewAction | None:
        view = self.query_one("#settings-controls", ListView)
        if view.index is None or not (0 <= view.index < len(view.children)):
            return None
        item = view.children[view.index]
        return item.action if isinstance(item, ControlSettingItem) else None

    def _show_footer(self, message: str) -> None:
        self.query_one("#settings-footer", Static).update(Text(message, no_wrap=True))

    def _show_default_footer(self) -> None:
        message = {
            "help": "j/k scroll · h/l tabs · esc",
            "controls": "j/k · enter bind · bs default · esc",
            "sections": (
                "j/k · space change · h/l tabs · esc"
                if self.card is not None
                else "h/l tabs · esc"
            ),
        }[self.tab]
        self._show_footer(message)

    def _refresh_control_bindings(self) -> None:
        for item in self.query(ControlSettingItem):
            item.refresh_binding(self.repetui.review_controls)

    def _apply_review_controls(self, controls: ReviewControls) -> bool:
        try:
            self.repetui.save_review_controls(controls)
        except OSError:
            self.capturing = None
            self.pending_binding = None
            self._show_footer("[err] controls not saved")
            return False
        self.capturing = None
        self.pending_binding = None
        self._refresh_control_bindings()
        self._show_default_footer()
        return True

    def _propose_control_binding(self, action: ReviewAction, key: str) -> None:
        try:
            controls = self.repetui.review_controls.with_binding(action, key)
        except BindingConflict as conflict:
            self.capturing = None
            self.pending_binding = PendingControlBinding(action, key, conflict.action)
            self._show_footer(
                f"[!] {key} = {conflict.action.label} · y replace · n cancel"
            )
            return
        except ValueError:
            self._show_footer(f"[fixed] {key} stays navigation")
            return
        self._apply_review_controls(controls)

    def on_key(self, event: events.Key) -> None:
        if self.pending_binding is not None:
            event.stop()
            event.prevent_default()
            key = event.character if event.is_printable else event.key
            if key == "y":
                pending = self.pending_binding
                controls = self.repetui.review_controls.with_binding(
                    pending.action,
                    pending.key,
                    replace=True,
                )
                self._apply_review_controls(controls)
            elif key in {"n", "escape"}:
                self.pending_binding = None
                self._show_default_footer()
            return
        if self.capturing is None:
            if self.tab == "controls" and event.key == "enter":
                event.stop()
                event.prevent_default()
                self.action_cycle()
            elif self.tab == "controls" and event.key == "backspace":
                event.stop()
                event.prevent_default()
                action = self._selected_control_action()
                if action is not None:
                    self._propose_control_binding(
                        action, DEFAULT_REVIEW_BINDINGS[action]
                    )
            return
        event.stop()
        event.prevent_default()
        action = self.capturing
        if event.key == "backspace":
            self.capturing = None
            self._propose_control_binding(action, DEFAULT_REVIEW_BINDINGS[action])
            return
        key = (
            event.key
            if event.key == "space"
            else event.character if event.is_printable else event.key
        )
        if key == "escape":
            self.capturing = None
            self._show_default_footer()
            return
        self._propose_control_binding(action, key)

    def action_back(self) -> None:
        if self.capturing is not None or self.pending_binding is not None:
            self.capturing = None
            self.pending_binding = None
            self._show_default_footer()
            return
        self.app.pop_screen()
        if self.review is not None:
            self.review.preferences_changed()


class ReviewContent(Static):
    """Card document that reports its gutter-adjusted width to the screen."""

    def on_resize(self) -> None:
        screen = self.screen
        if isinstance(screen, ReviewScreen):
            screen.renderable_width_changed(self.size.width)


class ReviewScreen(Screen[None]):
    RATING_FEEDBACK_DURATION = 1.0

    BINDINGS = [
        Binding("escape", "back", "Decks", show=False),
        Binding(
            "enter",
            "primary",
            "Reveal/Good",
            show=False,
            id="review.reveal_good",
        ),
        Binding("space", "toggle_fold", "Reveal/expand", show=False),
        Binding("1", "again", "Again", show=False, id="review.again"),
        Binding("2", "hard", "Hard", show=False, id="review.hard"),
        Binding("3", "good", "Good", show=False, id="review.good"),
        Binding("4", "easy", "Easy", show=False, id="review.easy"),
        Binding("u", "undo", "Undo", show=False, id="review.undo"),
        Binding("b", "bury", "Bury", show=False, id="review.bury"),
        Binding("x", "suspend", "Suspend", show=False, id="review.suspend"),
        Binding("f", "flag", "Flag", show=False, id="review.flag"),
        Binding("j", "scroll_down", "Scroll down", show=False),
        Binding("k", "scroll_up", "Scroll up", show=False),
        Binding("g", "scroll_top", "Top", show=False),
        Binding("G", "scroll_bottom", "Bottom", show=False),
        Binding("s", "sync", "Sync", show=False, id="review.sync"),
    ]

    def __init__(self, deck: Deck) -> None:
        super().__init__()
        self.deck = deck
        self.card: ReviewCard | None = None
        self.revealed = False
        self.expanded_sections: set[str] = set()
        self.selected_folded = 0
        self._rendered_card_width = 0
        self._rating_feedback: int | None = None
        self._rating_feedback_timer: Timer | None = None

    @property
    def repetui(self) -> RepetuiApp:
        return cast("RepetuiApp", self.app)

    def compose(self) -> ComposeResult:
        yield Vertical(
            VerticalScroll(ReviewContent(id="card"), id="card-scroll"),
            Static(id="review-actions"),
            id="review-layout",
        )

    def on_mount(self) -> None:
        self.repetui.backend.begin_review(self.deck.id)
        self.load_next()

    def on_unmount(self) -> None:
        if self._rating_feedback_timer is not None:
            self._rating_feedback_timer.stop()
            self._rating_feedback_timer = None

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
            self._refresh_action_row(actions)
            return

        renderable_width = content.size.width or self.size.width
        self._rendered_card_width = max(renderable_width, 1)
        flow = compose_review(
            self.card.presentation,
            self.deck.name,
            counts,
            self._rendered_card_width,
            revealed=self.revealed,
            sections=self._section_states() if self.revealed else (),
            current_queue=self.card.queue,
            answer_layout=self.repetui.preferences.answer_layout(
                self.card.presentation.identity
            ),
        )
        self._refresh_action_row(actions)
        content.update(flow)
        if reset_scroll:
            self.query_one("#card-scroll", VerticalScroll).scroll_home(animate=False)

    def _refresh_action_row(self, actions: Static) -> None:
        if self.card is not None and self.revealed:
            actions.update(
                compose_ratings(self.size.width, self.repetui.review_controls)
            )
            actions.display = True
        elif self._rating_feedback is not None:
            actions.update(compose_rating_feedback(self._rating_feedback))
            actions.display = True
        else:
            actions.display = False

    def _set_rating_feedback(self, rating: int) -> None:
        if self._rating_feedback_timer is not None:
            self._rating_feedback_timer.stop()
        self._rating_feedback = rating
        self._rating_feedback_timer = self.set_timer(
            self.RATING_FEEDBACK_DURATION,
            self._clear_rating_feedback,
        )

    def _clear_rating_feedback(self) -> None:
        self._rating_feedback = None
        self._rating_feedback_timer = None
        if self.is_mounted:
            self._refresh_view(reset_scroll=False)

    def renderable_width_changed(self, actual_width: int) -> None:
        """Recompose after Textual adds or removes the scrollbar gutter."""
        if (
            self.card is not None
            and self.is_mounted
            and actual_width > 0
            and actual_width != self._rendered_card_width
        ):
            self._refresh_view(reset_scroll=False)

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
            self._set_rating_feedback(rating)
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

    def _show_operation_status(self, message: str, *, success: bool) -> None:
        self.app.push_screen(OperationStatusPill(message, success=success))

    def action_undo(self) -> None:
        if self._busy():
            return
        try:
            undone = self.repetui.backend.undo()
        except Exception:
            self._show_operation_status("[err] undo failed", success=False)
            return
        if not undone:
            self._show_operation_status("[err] nothing to undo", success=False)
            return
        try:
            self.load_next()
        except Exception:
            self._show_refresh_failure(clear_card=True)
            return
        self._show_operation_status("[ok] undone", success=True)

    def _show_refresh_failure(self, *, clear_card: bool) -> None:
        if clear_card:
            with contextlib.suppress(Exception):
                self.repetui.backend.begin_review(self.deck.id)
            self.card = None
            self.revealed = False
            self.expanded_sections.clear()
            self.selected_folded = 0
            content = Text("review · ", style="#dc6b72")
            content.append(self.deck.leaf_name, style="bold #eee9e0")
            content.append("\nCould not refresh cards.", style="#aaa49b")
            self.query_one("#card", Static).update(content)
            self.query_one("#review-actions", Static).display = False
        self._show_operation_status("[err] refresh failed", success=False)

    def _advance_after_operation(
        self,
        operation: Callable[[], None],
        *,
        success_message: str,
        failure_message: str,
    ) -> None:
        if self._busy():
            return
        try:
            operation()
        except Exception:
            self._show_operation_status(failure_message, success=False)
            return
        try:
            self.load_next()
        except Exception:
            self._show_refresh_failure(clear_card=True)
            return
        self._show_operation_status(success_message, success=True)

    def action_bury(self) -> None:
        self._advance_after_operation(
            self.repetui.backend.bury_current,
            success_message="[ok] buried",
            failure_message="[err] bury failed",
        )

    def action_suspend(self) -> None:
        self._advance_after_operation(
            self.repetui.backend.suspend_current,
            success_message="[ok] suspended",
            failure_message="[err] suspend failed",
        )

    def action_flag(self) -> None:
        if self._busy() or self.card is None:
            return
        self.app.push_screen(FlagSelectionPill(), self._flag_selected)

    def _flag_selected(self, flag: int | None) -> None:
        if flag is None:
            return
        try:
            self.repetui.backend.set_current_flag(flag)
        except Exception:
            self._show_operation_status("[err] flag failed", success=False)
            return
        try:
            self._refresh_view(reset_scroll=False)
        except Exception:
            self._show_refresh_failure(clear_card=False)
            return
        message = "[ok] flag clear" if flag == 0 else f"[ok] flag {flag}"
        self._show_operation_status(message, success=True)

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


class StatusPill(ModalScreen[None]):
    """Reusable centered one-line terminal status surface."""

    SURFACE_ID = "status-pill"

    def __init__(self, message: str) -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        yield Static(
            Text(self._message, no_wrap=True),
            id=self.SURFACE_ID,
            classes="status-pill",
        )

    def on_mount(self) -> None:
        self._fit_surface()

    def on_resize(self) -> None:
        if self.is_mounted:
            self._fit_surface()

    def _show_message(self, message: str) -> None:
        self._message = message
        self.query_one(".status-pill", Static).update(Text(message, no_wrap=True))
        self._fit_surface()

    def _fit_surface(self) -> None:
        """Keep padding and surroundings until the state marker needs the cells."""
        surface = self.query_one(".status-pill", Static)
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


class OperationStatusPill(StatusPill):
    """Brief review-operation result using the shared status surface."""

    BINDINGS = [
        Binding("q", "block", show=False, priority=True),
        Binding("question_mark", "block", show=False, priority=True),
    ]

    def __init__(self, message: str, *, success: bool) -> None:
        super().__init__(message)
        self.success = success
        self._dismiss_timer: Timer | None = None

    def on_mount(self) -> None:
        super().on_mount()
        self.query_one(".status-pill").add_class("-success" if self.success else "-error")
        self._dismiss_timer = self.set_timer(1.0, self._dismiss_status)

    def on_unmount(self) -> None:
        if self._dismiss_timer is not None:
            self._dismiss_timer.stop()
            self._dismiss_timer = None

    def action_block(self) -> None:
        """Keep global shortcuts from leaking through the brief status state."""

    def _dismiss_status(self) -> None:
        self.dismiss()


class FlagSelectionPill(StatusPill):
    """Compact modal state for clearing or selecting an Anki card flag."""

    BINDINGS = [
        *(Binding(str(flag), f"select_flag({flag})", show=False) for flag in range(8)),
        Binding("escape", "cancel", show=False),
        Binding("q", "block", show=False, priority=True),
        Binding("question_mark", "block", show=False, priority=True),
    ]

    def __init__(self) -> None:
        super().__init__("[?] flag 0–7 · esc")

    def action_select_flag(self, flag: int) -> None:
        self.dismiss(flag)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_block(self) -> None:
        """Keep global shortcuts inside the flag-selection state."""


class SyncPopup(StatusPill):
    """One modal, terminal-native sync message over the originating screen."""

    SPINNER_FRAMES = ("|", "/", "-", "\\")
    SPINNER_INTERVAL = 0.12
    SURFACE_ID = "sync-popup"

    BINDINGS = [
        Binding("q", "block", show=False, priority=True),
        Binding("question_mark", "block", show=False, priority=True),
        Binding("s", "block", show=False),
        Binding("escape", "dismiss_failure", show=False),
        Binding("enter", "dismiss_failure", show=False),
    ]

    def __init__(self) -> None:
        super().__init__("[|] syncing...")
        self._frame_index = 0
        self._spinner_timer: Timer | None = None
        self._dismiss_timer: Timer | None = None
        self._failure_dismissible = False
        self._fatal = False

    def on_mount(self) -> None:
        super().on_mount()
        self._spinner_timer = self.set_interval(self.SPINNER_INTERVAL, self.advance_spinner)

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

    #deck-header, #error-header {
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
        scrollbar-gutter: stable;
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

    #settings-help, #settings-sections, #settings-sections-empty, #settings-controls {
        height: 1fr;
        background: #111416;
        scrollbar-size-vertical: 1;
    }

    #settings-sections-empty {
        color: #aaa49b;
    }

    AnswerLayoutSettingItem, SectionSettingItem, ControlSettingItem {
        height: 1;
        layout: horizontal;
    }

    AnswerLayoutSettingItem.-highlight,
    SectionSettingItem.-highlight,
    ControlSettingItem.-highlight {
        background: #293034;
    }

    .setting-label {
        width: 1fr;
        height: 1;
    }

    .setting-mode {
        width: 7;
        height: 1;
        text-align: right;
    }

    .control-label {
        width: 1fr;
        height: 1;
    }

    .control-binding {
        width: 9;
        height: 1;
        text-align: right;
    }

    .surface-footer {
        height: 1;
        color: #817d76;
        overflow: hidden;
    }

    #error-layout {
        width: 100%;
        height: 100%;
        background: #111416;
    }

    #error-scroll {
        height: 1fr;
        scrollbar-size-vertical: 1;
    }

    #error-header, #error-scroll {
        color: #dc6b72;
    }

    SyncPopup, OperationStatusPill, FlagSelectionPill {
        align: center middle;
        overflow: hidden;
        background: transparent;
    }

    .status-pill {
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

    .status-pill.-success {
        color: #79c98b;
    }

    .status-pill.-error {
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
        self.review_controls = self.preferences.review_controls(profile)
        self.set_keymap(self.review_controls.keymap())
        self.syncer = syncer
        self.syncing = False
        self._sync_origin: Screen[None] | None = None
        self._sync_popup: SyncPopup | None = None
        self._sync_thread: Thread | None = None
        self._sync_fatal_error: str | None = None
        self._shutdown_requested = Event()
        self._backend_lock = Lock()

    def save_review_controls(self, controls: ReviewControls) -> None:
        """Persist and activate one complete profile-scoped review keymap."""
        self.preferences.set_review_controls(self.profile, controls)
        self.review_controls = controls
        self.set_keymap(controls.keymap())

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
        if isinstance(screen, SettingsScreen):
            screen.action_back()
        elif isinstance(screen, ReviewScreen) and screen.card is not None:
            self.push_screen(SettingsScreen(screen, initial_tab="sections"))
        else:
            self.push_screen(SettingsScreen(initial_tab="help"))

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
        self._sync_fatal_error = result.reopen_error
        if self._sync_popup is not None:
            self._sync_popup.finish(result)

    def _sync_popup_closed(self, fatal: bool | None) -> None:
        fatal_error = self._sync_fatal_error
        if self.backend.is_open:
            for screen in self.screen_stack:
                if hasattr(screen, "backend_refreshed"):
                    cast(Refreshable, screen).backend_refreshed()
        self.syncing = False
        self._sync_popup = None
        self._sync_origin = None
        self._sync_fatal_error = None
        if fatal and fatal_error is not None:
            self.push_screen(ErrorScreen(f"Could not reopen the Anki collection: {fatal_error}"))
