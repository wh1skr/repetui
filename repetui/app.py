"""The complete, deliberately small repetui interface."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from enum import Enum
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
from textual.widgets import Input, ListItem, ListView, Static

from . import __version__
from .addons import (
    AddOnDefinition,
    AddOnEvent,
    AddOnEventType,
    AddOnManager,
    NumberSetting,
    PresentationCue,
    PresentationCueType,
    SettingDefinition,
    bundled_add_ons,
)
from .backend import AnkiBackend, BackendError, CollectionInUseError, Deck, ReviewCard
from .completion import completion_duration_seconds, compose_completion_frame
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
from .presentation import (
    CardTemplateIdentity,
    PresentationSection,
    SourceField,
    TemplateFieldProfile,
    default_field_profile,
    present_card,
)
from .recovery import CollectionOwner, InstanceControl, find_owner, force_close, request_close
from .sync import (
    FullSyncDirection,
    SyncOutcome,
    SyncStatus,
    failed_sync_outcome,
    full_sync_profile,
    sync_profile,
)


class Refreshable(Protocol):
    def backend_refreshed(self) -> None: ...


class CompletionCelebrationScreen(Screen[None]):
    """Brief full-pane presentation that consumes the key used to skip it."""

    FRAME_SECONDS = 0.08
    BINDINGS = [
        Binding("q", "skip", "Skip", show=False, priority=True),
        Binding("question_mark", "skip", "Skip", show=False, priority=True),
    ]

    def __init__(self, deck_name: str, duration: float) -> None:
        super().__init__()
        self.deck_name = deck_name
        self.duration = duration
        self.phase = 0
        self._frame_timer: Timer | None = None
        self._finish_timer: Timer | None = None
        self._discard_on_resume = False

    def compose(self) -> ComposeResult:
        yield Static(id="completion-art")

    def on_mount(self) -> None:
        self._render_frame()
        self._frame_timer = self.set_interval(self.FRAME_SECONDS, self._advance_frame)
        self._finish_timer = self.set_timer(self.duration, self._finish)

    def on_resize(self) -> None:
        if self.is_mounted:
            self._render_frame()

    def on_unmount(self) -> None:
        self.stop_animation()

    def on_screen_suspend(self) -> None:
        self._discard_on_resume = True
        self.stop_animation()

    def on_screen_resume(self) -> None:
        if self._discard_on_resume:
            self.call_after_refresh(self._dismiss_effect)

    def stop_animation(self) -> None:
        """Release every timer owned by the transient effect."""
        for timer in (self._frame_timer, self._finish_timer):
            if timer is not None:
                timer.stop()
        self._frame_timer = None
        self._finish_timer = None

    def _render_frame(self) -> None:
        art = self.query_one("#completion-art", Static)
        width = art.size.width or self.size.width
        height = art.size.height or self.size.height
        art.update(
            compose_completion_frame(width, height, self.deck_name, self.phase)
        )

    def _advance_frame(self) -> None:
        self.phase += 1
        if self.is_mounted:
            self._render_frame()

    def _finish(self) -> None:
        self._finish_timer = None
        self._dismiss_effect()

    def action_skip(self) -> None:
        self._dismiss_effect()

    def on_key(self, event: events.Key) -> None:
        event.stop()
        event.prevent_default()
        self._dismiss_effect()

    def _dismiss_effect(self) -> None:
        if self.is_mounted:
            cast("RepetuiApp", self.app).close_completion_celebration(self)


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


class InstanceCloseRequested(Message):
    """A verified peer requested an ordinary app exit."""


class StartupRecoveryScreen(ErrorScreen):
    """Bounded, explicit recovery for a collection-in-use startup failure."""

    BINDINGS = [
        Binding("r", "retry", show=False),
        Binding("c", "close_owner", show=False),
        Binding("f", "confirm_force", show=False),
        Binding("escape", "cancel", show=False),
    ]

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.owner: CollectionOwner | None = None
        self._working = False
        self._cancelled = False
        self._force_available = False
        self._confirming = False
        self._completed = False

    def compose(self) -> ComposeResult:
        with Vertical(id="error-layout"):
            yield Static("repetui · collection in use", id="error-header")
            with VerticalScroll(id="error-scroll"):
                yield Static(Text(self.message), id="recovery-message")
                yield Input(placeholder="Type FORCE to confirm", id="force-confirm")
            yield Static("r retry · c close other · q quit", classes="surface-footer")

    def on_mount(self) -> None:
        self.run_worker(self._discover())

    def on_unmount(self) -> None:
        self._cancelled = True

    def on_resize(self) -> None:
        if self.is_mounted and self._confirming:
            self.call_after_refresh(
                self.query_one("#force-confirm", Input).scroll_visible, animate=False
            )

    def _show(self, message: str) -> None:
        if self.is_mounted:
            self.query_one("#recovery-message", Static).update(Text(message))

    async def _discover(self) -> None:
        self._working = True
        try:
            owner = await asyncio.to_thread(
                find_owner, cast("RepetuiApp", self.app).profile.collection
            )
            if not self.is_mounted:
                return
            self.owner = owner
            self._show(
                f"{owner.application} · PID {owner.pid}\n"
                "c: close this instance and retry\nNo other collection will be closed."
                if owner else
                "Owner cannot be verified here.\nClose it manually, then press r.\n"
                "Windows-host/macOS recovery is unsupported."
            )
        finally:
            self._working = False

    def action_retry(self) -> None:
        self._start("retry")

    def action_close_owner(self) -> None:
        if self.owner is not None:
            self._start("close")

    def _start(self, mode: str) -> None:
        if self._working or self._confirming or self._completed:
            return
        self._working = True
        self._cancelled = False
        self.run_worker(self._recover(mode), group="startup-recovery")

    def _try_open(self, app: RepetuiApp) -> None:
        with app._backend_lock:
            if app._shutdown_requested.is_set() or self._cancelled:
                return
            app.backend.open()

    async def _recover(self, mode: str) -> None:
        app = cast("RepetuiApp", self.app)
        owner = self.owner
        self._show("Retrying… Esc cancels waiting.")
        try:
            if mode != "retry":
                operation = force_close if mode == "force" else request_close
                if owner is None or not await asyncio.to_thread(
                    operation, app.profile.collection, owner
                ):
                    current = await asyncio.to_thread(find_owner, app.profile.collection)
                    self._force_available = current is not None and current == owner
                    self.owner = current
                    self._show(
                        "Closure unavailable or owner changed.\n"
                        + (f"{current.application} · PID {current.pid}\n" if current else "")
                        + "Close manually and press r.\n"
                        + ("f: review force-close warning" if self._force_available
                           else "c: inspect the current owner again")
                    )
                    return
            for _ in range(20 if mode != "retry" else 1):
                if self._cancelled or not self.is_mounted:
                    return
                try:
                    await asyncio.to_thread(self._try_open, app)
                except CollectionInUseError:
                    await asyncio.sleep(0.15)
                    continue
                except BackendError as exc:
                    self._show(str(exc))
                    return
                if self._cancelled or not self.is_mounted:
                    with app._backend_lock:
                        app.backend.close()
                    return
                if app.backend.is_open:
                    self._completed = True
                    app.call_later(app.startup_ready, replace=True)
                    return
            current = await asyncio.to_thread(find_owner, app.profile.collection)
            self.owner = current
            self._force_available = current is not None and current == owner
            self._show(
                "Still in use. r: retry manually\n"
                + (f"{current.application} · PID {current.pid}\n" if current else "")
                + ("f: review force-close warning" if self._force_available else "c: inspect owner")
            )
        finally:
            self._working = False

    def action_confirm_force(self) -> None:
        if self._working or not self._force_available or self.owner is None:
            return
        self._confirming = True
        self._show(
            f"Force close {self.owner.application} PID {self.owner.pid}?\n"
            "May lose unsaved work or damage data.\n"
            "Type FORCE and Enter; Esc cancels."
        )
        field = self.query_one("#force-confirm", Input)
        field.display = True
        field.value = ""
        field.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        if not self._confirming or event.value != "FORCE":
            return
        self._confirming = False
        self.query_one("#force-confirm").display = False
        self._start("force")

    def action_cancel(self) -> None:
        if self._confirming:
            self._confirming = False
            self.query_one("#force-confirm").display = False
        self._cancelled = True
        self._show(
            "Waiting cancelled; a close request\nmay already have been sent. r: retry"
            if self._working else "Cancelled. r: retry · c: close other"
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


class TemplateFieldsSettingItem(ListItem):
    """Entry point for editing the current template's field profile."""

    def compose(self) -> ComposeResult:
        yield Static("card fields", classes="setting-label")
        yield Static("edit", classes="setting-mode")


class FieldRole(str, Enum):
    AUTO = "auto"
    IGNORE = "ignore"
    PROMPT = "prompt"
    ANSWER = "answer"

    @property
    def next(self) -> FieldRole:
        roles = (FieldRole.AUTO, FieldRole.PROMPT, FieldRole.ANSWER, FieldRole.IGNORE)
        return roles[(roles.index(self) + 1) % len(roles)]


class FieldProfileItem(ListItem):
    """One source field and its terminal presentation role."""

    def __init__(self, field: SourceField, role: FieldRole) -> None:
        super().__init__()
        self.field = field
        self.role = role

    def compose(self) -> ComposeResult:
        yield Static(classes="field-name")
        yield Static(classes="field-role")

    def on_mount(self) -> None:
        self.refresh_role()

    def refresh_role(self) -> None:
        colour = {
            FieldRole.AUTO: "#aaa49b",
            FieldRole.IGNORE: "#817d76",
            FieldRole.PROMPT: "#68a8df",
            FieldRole.ANSWER: "#79c98b",
        }[self.role]
        self.query_one(".field-name", Static).update(
            Text(self.field.name, style="#d9d5ce", overflow="ellipsis", no_wrap=True)
        )
        self.query_one(".field-role", Static).update(
            Text(self.role.value, style=colour, no_wrap=True)
        )

    def swap_with(self, other: FieldProfileItem) -> None:
        self.field, other.field = other.field, self.field
        self.role, other.role = other.role, self.role
        self.refresh_role()
        other.refresh_role()


class TemplateFieldSetupScreen(Screen[None]):
    """One-time, editable mapping from Anki fields to terminal roles."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("j", "down", "Down", show=False),
        Binding("k", "up", "Up", show=False),
        Binding("space", "cycle_role", "Role", show=False),
        Binding("J", "move_down", "Move down", show=False),
        Binding("K", "move_up", "Move up", show=False),
        Binding("enter", "save", "Save", show=False, priority=True),
    ]

    def __init__(
        self,
        review: ReviewScreen,
        profile: TemplateFieldProfile,
    ) -> None:
        super().__init__()
        self.review = review
        self.profile = profile

    def compose(self) -> ComposeResult:
        assert self.review.card is not None
        raw = self.review.card.raw_content
        assert raw is not None
        prompt = set(self.profile.prompt_fields)
        answer = set(self.profile.answer_fields)
        ignored = set(self.profile.ignored_fields)
        by_name = {field.name: field for field in raw.fields}
        selected_order = self.profile.prompt_fields + self.profile.answer_fields
        ordered_fields = tuple(
            by_name[name] for name in selected_order if name in by_name
        ) + tuple(field for field in raw.fields if field.name not in selected_order)
        rows = []
        for field in ordered_fields:
            role = (
                FieldRole.PROMPT
                if field.name in prompt
                else FieldRole.ANSWER
                if field.name in answer
                else FieldRole.IGNORE
                if field.name in ignored
                else FieldRole.AUTO
            )
            rows.append(FieldProfileItem(field, role))
        yield Vertical(
            Static("adapt card fields", id="field-profile-header"),
            ListView(*rows, id="field-profile-fields"),
            Static(
                "space role · J/K order · enter save",
                id="field-profile-footer",
                classes="surface-footer",
            ),
            id="field-profile-layout",
        )

    def on_mount(self) -> None:
        fields = self.query_one("#field-profile-fields", ListView)
        if fields.children:
            fields.index = 0
        fields.focus()

    def _view(self) -> ListView:
        return self.query_one("#field-profile-fields", ListView)

    def _selected(self) -> FieldProfileItem | None:
        view = self._view()
        if view.index is None or not (0 <= view.index < len(view.children)):
            return None
        item = view.children[view.index]
        return item if isinstance(item, FieldProfileItem) else None

    def action_down(self) -> None:
        self._view().action_cursor_down()

    def action_up(self) -> None:
        self._view().action_cursor_up()

    def action_cycle_role(self) -> None:
        item = self._selected()
        if item is not None:
            item.role = item.role.next
            item.refresh_role()

    def _move(self, offset: int) -> None:
        view = self._view()
        if view.index is None:
            return
        destination = view.index + offset
        if not (0 <= destination < len(view.children)):
            return
        current = view.children[view.index]
        other = view.children[destination]
        if isinstance(current, FieldProfileItem) and isinstance(other, FieldProfileItem):
            current.swap_with(other)
            view.index = destination

    def action_move_down(self) -> None:
        self._move(1)

    def action_move_up(self) -> None:
        self._move(-1)

    def action_save(self) -> None:
        rows = tuple(self.query(FieldProfileItem))
        profile = TemplateFieldProfile(
            tuple(row.field.name for row in rows if row.role is FieldRole.PROMPT),
            tuple(row.field.name for row in rows if row.role is FieldRole.ANSWER),
            tuple(row.field.name for row in rows if row.role is FieldRole.IGNORE),
        )
        if not profile.prompt_fields or not profile.answer_fields:
            self.notify("Choose at least one prompt and answer field.", severity="warning")
            return
        try:
            self.review.apply_field_profile(profile)
        except OSError:
            self.query_one("#field-profile-footer", Static).update(
                Text("[err] field profile not saved", style="#dc6b72", no_wrap=True)
            )
            return
        self.app.pop_screen()

    def action_cancel(self) -> None:
        self.app.pop_screen()


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


class AddOnItem(ListItem):
    """One registered add-on and its profile-scoped enabled state."""

    def __init__(self, definition: AddOnDefinition) -> None:
        super().__init__()
        self.definition = definition

    def compose(self) -> ComposeResult:
        yield Static(self.definition.name, classes="add-on-label")
        yield Static(classes="add-on-state")

    def refresh_state(self, manager: AddOnManager) -> None:
        self.query_one(".add-on-state", Static).update(
            _on_off_text(manager.is_enabled(self.definition.id))
        )


def _on_off_text(enabled: bool) -> Text:
    return Text("on" if enabled else "off", style="#79c98b" if enabled else "#aaa49b")


class AddOnSettingItem(ListItem):
    """One repetui-rendered enabled or declarative setting row."""

    def __init__(
        self,
        setting: SettingDefinition | None = None,
        value: str | int | bool = False,
    ) -> None:
        super().__init__()
        self.setting = setting
        self.value = value

    def compose(self) -> ComposeResult:
        yield Static(
            "enabled" if self.setting is None else self.setting.label,
            classes="add-on-setting-label",
        )
        yield Static(
            self._rendered_value(self.value),
            classes="add-on-setting-value",
        )

    def _rendered_value(self, value: str | int | bool) -> Text:
        if isinstance(value, bool):
            return _on_off_text(value)
        style = "#d7b85a" if isinstance(self.setting, NumberSetting) else "#d9d5ce"
        return Text(str(value), style=style, no_wrap=True)

    def refresh_value(self, manager: AddOnManager, add_on_id: str) -> None:
        if self.setting is None:
            value: str | int | bool = manager.is_enabled(add_on_id)
        else:
            value = manager.setting_values(add_on_id)[self.setting.id]
        self.value = value
        self.query_one(".add-on-setting-value", Static).update(
            self._rendered_value(value)
        )


@dataclass(frozen=True)
class PendingControlBinding:
    action: ReviewAction
    key: str
    conflict: ReviewAction


class SettingsScreen(Screen[None]):
    """One tiny-pane-safe home for help, controls, and card sections."""

    TABS = ("help", "controls", "sections", "add-ons")

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
        self.add_on_detail: AddOnDefinition | None = None
        self.capturing: ReviewAction | None = None
        self.pending_binding: PendingControlBinding | None = None

    @property
    def repetui(self) -> RepetuiApp:
        return cast("RepetuiApp", self.app)

    def _field_profile_for_editor(self) -> TemplateFieldProfile | None:
        if self.card is None or self.card.raw_content is None:
            return None
        return (
            self.repetui.preferences.field_profile(self.card.identity)
            or self.card.presentation.suggested_profile
            or default_field_profile(self.card.raw_content, self.card.presentation)
        )

    def compose(self) -> ComposeResult:
        sections = self.card.presentation.back.sections if self.card is not None else ()
        can_edit_fields = self._field_profile_for_editor() is not None
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
                *(TemplateFieldsSettingItem(),) if can_edit_fields else (),
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
            ListView(
                *(AddOnItem(definition) for definition in self.repetui.add_ons.definitions),
                id="settings-add-ons",
            ),
            Static("no add-ons bundled", id="settings-add-ons-empty"),
            ListView(id="settings-add-on-detail"),
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
        for item in self.query(AddOnItem):
            item.refresh_state(self.repetui.add_ons)
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
        add_ons = self.query_one("#settings-add-ons", ListView)
        add_ons_empty = self.query_one("#settings-add-ons-empty", Static)
        add_on_detail = self.query_one("#settings-add-on-detail", ListView)
        help_scroll.display = tab == "help"
        sections.display = tab == "sections" and self.card is not None
        sections_empty.display = tab == "sections" and self.card is None
        controls.display = tab == "controls"
        showing_add_ons = tab == "add-ons" and self.add_on_detail is None
        add_ons.display = showing_add_ons and bool(add_ons.children)
        add_ons_empty.display = showing_add_ons and not add_ons.children
        add_on_detail.display = tab == "add-ons" and self.add_on_detail is not None
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
        elif tab == "add-ons":
            target = add_on_detail if self.add_on_detail is not None else add_ons
            if target.children and target.index is None:
                target.index = 0
            target.focus()
        elif self.card is not None:
            if sections.children and sections.index is None:
                sections.index = 0
            sections.focus()
        self._show_default_footer()

    def action_previous_tab(self) -> None:
        self.add_on_detail = None
        index = self.TABS.index(self.tab)
        self._show_tab(self.TABS[(index - 1) % len(self.TABS)])

    def action_next_tab(self) -> None:
        self.add_on_detail = None
        index = self.TABS.index(self.tab)
        self._show_tab(self.TABS[(index + 1) % len(self.TABS)])

    def action_down(self) -> None:
        if self.tab == "help":
            self.query_one("#settings-help", VerticalScroll).scroll_down(animate=False)
        elif self.tab == "sections" and self.card is not None:
            self.query_one("#settings-sections", ListView).action_cursor_down()
        elif self.tab == "controls":
            self.query_one("#settings-controls", ListView).action_cursor_down()
        elif self.tab == "add-ons":
            self._active_add_on_view().action_cursor_down()

    def action_up(self) -> None:
        if self.tab == "help":
            self.query_one("#settings-help", VerticalScroll).scroll_up(animate=False)
        elif self.tab == "sections" and self.card is not None:
            self.query_one("#settings-sections", ListView).action_cursor_up()
        elif self.tab == "controls":
            self.query_one("#settings-controls", ListView).action_cursor_up()
        elif self.tab == "add-ons":
            self._active_add_on_view().action_cursor_up()

    def action_top(self) -> None:
        if self.tab == "help":
            self.query_one("#settings-help", VerticalScroll).scroll_home(animate=False)
            return
        if self.tab == "sections" and self.card is not None:
            view = self.query_one("#settings-sections", ListView)
        elif self.tab == "controls":
            view = self.query_one("#settings-controls", ListView)
        elif self.tab == "add-ons":
            view = self._active_add_on_view()
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
        elif self.tab == "add-ons":
            view = self._active_add_on_view()
        else:
            return
        if view.children:
            view.index = len(view.children) - 1

    def action_cycle(self) -> None:
        if self.tab == "add-ons":
            self._cycle_add_on_value()
            return
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
        if isinstance(item, TemplateFieldsSettingItem):
            profile = self._field_profile_for_editor()
            if profile is not None and self.review is not None:
                setup = TemplateFieldSetupScreen(self.review, profile)
                self.app.pop_screen()
                self.repetui.call_after_refresh(lambda: self.repetui.push_screen(setup))
            return
        if not isinstance(item, SectionSettingItem):
            return
        mode = self.repetui.preferences.mode(identity, item.section.id)
        self.repetui.preferences.set_mode(identity, item.section.id, mode.next)
        item.refresh_mode(self.repetui.preferences, identity)

    def _active_add_on_view(self) -> ListView:
        return self.query_one(
            "#settings-add-on-detail"
            if self.add_on_detail is not None
            else "#settings-add-ons",
            ListView,
        )

    def _selected_add_on(self) -> AddOnItem | None:
        view = self.query_one("#settings-add-ons", ListView)
        if view.index is None or not (0 <= view.index < len(view.children)):
            return None
        item = view.children[view.index]
        return item if isinstance(item, AddOnItem) else None

    def _open_selected_add_on(self) -> None:
        item = self._selected_add_on()
        if item is None:
            return
        self.add_on_detail = item.definition
        detail = self.query_one("#settings-add-on-detail", ListView)
        detail.clear()
        values = self.repetui.add_ons.setting_values(item.definition.id)
        detail.append(
            AddOnSettingItem(
                value=self.repetui.add_ons.is_enabled(item.definition.id)
            )
        )
        for setting in item.definition.settings:
            detail.append(AddOnSettingItem(setting, values[setting.id]))
        detail.index = 0
        self._show_tab("add-ons")

    def _cycle_add_on_value(self) -> None:
        if self.add_on_detail is None:
            item = self._selected_add_on()
            if item is None:
                return
            try:
                self.repetui.add_ons.set_enabled(
                    item.definition.id,
                    not self.repetui.add_ons.is_enabled(item.definition.id),
                )
            except OSError:
                self._show_footer("[err] add-on not saved")
                return
            item.refresh_state(self.repetui.add_ons)
            self._show_default_footer()
            return
        view = self.query_one("#settings-add-on-detail", ListView)
        if view.index is None or not (0 <= view.index < len(view.children)):
            return
        item = view.children[view.index]
        if not isinstance(item, AddOnSettingItem):
            return
        try:
            if item.setting is None:
                self.repetui.add_ons.set_enabled(
                    self.add_on_detail.id,
                    not self.repetui.add_ons.is_enabled(self.add_on_detail.id),
                )
                registry_item = self._selected_add_on()
                if registry_item is not None:
                    registry_item.refresh_state(self.repetui.add_ons)
            else:
                self.repetui.add_ons.cycle_setting(
                    self.add_on_detail.id, item.setting.id
                )
        except OSError:
            self._show_footer("[err] add-on not saved")
            return
        item.refresh_value(self.repetui.add_ons, self.add_on_detail.id)
        self._show_default_footer()

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
            "add-ons": (
                "j/k · space change · esc back"
                if self.add_on_detail is not None
                else "j/k · space toggle · enter settings"
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
            if self.tab == "add-ons" and event.key == "enter":
                event.stop()
                event.prevent_default()
                if self.add_on_detail is None:
                    self._open_selected_add_on()
                else:
                    self.action_cycle()
                return
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
        if self.tab == "add-ons" and self.add_on_detail is not None:
            self.add_on_detail = None
            self._show_tab("add-ons")
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
        if self.card is not None:
            self.repetui.dispatch_add_on_event(
                AddOnEvent(AddOnEventType.REVIEW_STARTED, deck_name=self.deck.name)
            )

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
        if self.card is not None and self.card.raw_content is not None:
            profile = self.repetui.preferences.field_profile(self.card.identity)
            self.card = replace(
                self.card,
                presentation=present_card(self.card.raw_content, profile),
            )
        self.revealed = False
        self.expanded_sections.clear()
        self.selected_folded = 0
        self._refresh_view()
        if self.card is not None and self.card.presentation.suggested_profile is not None:
            self.call_after_refresh(self._offer_field_setup)

    def _offer_field_setup(self) -> None:
        if self.card is None or self.app.screen is not self:
            return
        suggestion = self.card.presentation.suggested_profile
        if suggestion is None:
            return
        key = (self.card.identity.note_type_id, self.card.identity.template_ordinal)
        if key in self.repetui.offered_field_setups:
            return
        self.repetui.offered_field_setups.add(key)
        self.app.push_screen(TemplateFieldSetupScreen(self, suggestion))

    def apply_field_profile(self, profile: TemplateFieldProfile) -> None:
        if self.card is None or self.card.raw_content is None:
            return
        self.repetui.preferences.set_field_profile(self.card.identity, profile)
        self.card = replace(
            self.card,
            presentation=present_card(self.card.raw_content, profile),
        )
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
        if self.repetui.syncing:
            return
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
            self.repetui.dispatch_add_on_event(
                AddOnEvent(
                    AddOnEventType.RATING_ACCEPTED,
                    deck_name=self.deck.name,
                    rating=rating,
                )
            )
            if self.card is None:
                self.repetui.dispatch_add_on_event(
                    AddOnEvent(
                        AddOnEventType.REVIEW_COMPLETED,
                        deck_name=self.deck.name,
                    )
                )
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
    """Sync status and persistent conflict guidance over the originating screen."""

    SPINNER_FRAMES = ("|", "/", "-", "\\")
    SPINNER_INTERVAL = 0.12
    SURFACE_ID = "sync-popup"

    BINDINGS = [
        Binding("q", "block", show=False, priority=True),
        Binding("question_mark", "block", show=False, priority=True),
        Binding("s", "block", show=False),
        Binding("escape", "dismiss_failure", show=False),
        Binding("enter", "dismiss_failure", show=False),
        Binding("d", "choose_download", show=False),
        Binding("u", "choose_upload", show=False),
    ]

    def __init__(self) -> None:
        super().__init__("[|] syncing...")
        self._frame_index = 0
        self._spinner_timer: Timer | None = None
        self._dismiss_timer: Timer | None = None
        self._failure_dismissible = False
        self._fatal = False
        self._conflict = False
        self._direction: FullSyncDirection | None = None

    def compose(self) -> ComposeResult:
        yield from super().compose()
        with VerticalScroll(id="sync-recovery"):
            yield Static(
                Text(
                    "[err] full sync required\n"
                    "Cards were not synced.\n"
                    "d: download web -> local\n"
                    "u: upload local -> web\n"
                    "Upload/download replaces one side.\n"
                    "Esc/Enter: back"
                )
            )
            yield Input(placeholder="Type the direction to confirm", id="sync-confirm")

    def action_choose_download(self) -> None:
        self._choose_direction(FullSyncDirection.DOWNLOAD)

    def on_resize(self) -> None:
        super().on_resize()
        if self.is_mounted and self._direction is not None:
            self.call_after_refresh(
                self.query_one("#sync-confirm", Input).scroll_visible, animate=False
            )

    def action_choose_upload(self) -> None:
        self._choose_direction(FullSyncDirection.UPLOAD)

    def _choose_direction(self, direction: FullSyncDirection) -> None:
        if not self._conflict or self._direction is not None:
            return
        self._direction = direction
        profile = cast("RepetuiApp", self.app).profile
        warning = (
            "Replaces LOCAL cards/review history.\n"
            "Local-only progress will be lost."
            if direction is FullSyncDirection.DOWNLOAD
            else "Replaces WEB cards/review history.\n"
            "Back up web-only progress first!"
        )
        self.query_one("#sync-recovery Static", Static).update(
            Text(
                f"Profile: {profile.name}\n{warning}\n"
                "Local backup first (not web/media).\n"
                f"Type {direction.value.upper()}; Esc: cancel"
            )
        )
        confirm = self.query_one("#sync-confirm", Input)
        confirm.display = True
        confirm.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        direction = self._direction
        if direction is None or event.value != direction.value.upper():
            return
        self._direction = None
        self._conflict = False
        self._failure_dismissible = False
        self.query_one("#sync-recovery").display = False
        self.query_one("#sync-confirm", Input).value = ""
        self.query_one("#sync-confirm").display = False
        surface = self.query_one("#sync-popup")
        surface.display = True
        surface.remove_class("-error", "-success")
        self._show_message("[|] backing up and syncing...")
        self._spinner_timer = self.set_interval(self.SPINNER_INTERVAL, self.advance_spinner)
        cast("RepetuiApp", self.app).start_full_sync(direction)

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
        if outcome.ok and result.reopen_error is None:
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
            if outcome.status is SyncStatus.FULL_SYNC_REQUIRED and not self._fatal:
                self._conflict = True
                self.query_one("#sync-popup").display = False
                recovery = self.query_one("#sync-recovery", VerticalScroll)
                recovery.display = True
                recovery.focus()
                return
            message = (
                "[err] collection unavailable"
                if self._fatal
                else {
                    SyncStatus.OFFLINE: "[err] offline",
                    SyncStatus.AUTH_REQUIRED: "[err] sign in through Anki",
                    SyncStatus.COLLECTION_UNAVAILABLE: "[err] collection unavailable",
                    SyncStatus.FAILED: "[err] sync failed",
                    SyncStatus.BACKUP_FAILED: "[err] backup failed; sync stopped",
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
        if self._direction is not None:
            # Enter is handled by the focused confirmation input; Escape
            # cancels without ever forwarding a direction to the worker.
            self._direction = None
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

    CompletionCelebrationScreen, #completion-art {
        width: 100%;
        height: 100%;
        background: #111416;
        overflow: hidden;
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

    #field-profile-layout {
        width: 100%;
        height: 100%;
        background: #111416;
    }

    #field-profile-header {
        height: 1;
        color: #eee9e0;
    }

    #field-profile-fields {
        height: 1fr;
        background: #111416;
        scrollbar-size-vertical: 1;
    }

    #field-profile-footer {
        height: 1;
    }

    FieldProfileItem {
        height: 1;
        layout: horizontal;
    }

    FieldProfileItem.-highlight {
        background: #293034;
    }

    .field-name {
        width: 1fr;
        height: 1;
    }

    .field-role {
        width: 7;
        height: 1;
        text-align: right;
    }

    #settings-header {
        height: 1;
        color: #eee9e0;
    }

    #settings-tabs {
        height: 1;
        color: #aaa49b;
    }

    #settings-help,
    #settings-sections,
    #settings-sections-empty,
    #settings-controls,
    #settings-add-ons,
    #settings-add-ons-empty,
    #settings-add-on-detail {
        height: 1fr;
        background: #111416;
        scrollbar-size-vertical: 1;
    }

    #settings-sections-empty, #settings-add-ons-empty {
        color: #aaa49b;
    }

    AnswerLayoutSettingItem,
    TemplateFieldsSettingItem,
    SectionSettingItem,
    ControlSettingItem,
    AddOnItem,
    AddOnSettingItem {
        height: 1;
        layout: horizontal;
    }

    AnswerLayoutSettingItem.-highlight,
    TemplateFieldsSettingItem.-highlight,
    SectionSettingItem.-highlight,
    ControlSettingItem.-highlight,
    AddOnItem.-highlight,
    AddOnSettingItem.-highlight {
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

    .add-on-label, .add-on-setting-label {
        width: 1fr;
        height: 1;
        overflow: hidden;
        text-overflow: ellipsis;
    }

    .add-on-state, .add-on-setting-value {
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

    #sync-recovery {
        display: none;
        width: 40;
        max-width: 100%;
        height: auto;
        max-height: 100%;
        padding: 0;
        background: #293034;
        color: #dc6b72;
        scrollbar-size-vertical: 1;
    }

    #sync-confirm, #force-confirm {
        display: none;
        height: 1;
        border: none;
        padding: 0;
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
        *,
        add_ons: Sequence[AddOnDefinition] | None = None,
        full_syncer: Callable[[ProfilePaths, FullSyncDirection], SyncOutcome] = full_sync_profile,
    ) -> None:
        super().__init__()
        self.backend = backend
        self.profile = profile
        self.preferences = preferences if preferences is not None else JsonPreferences()
        self.add_ons = AddOnManager(
            bundled_add_ons() if add_ons is None else add_ons,
            self.preferences,
            profile,
        )
        self.review_controls = self.preferences.review_controls(profile)
        self.set_keymap(self.review_controls.keymap())
        self.syncer = syncer
        self.full_syncer = full_syncer
        self.syncing = False
        self._sync_origin: Screen[None] | None = None
        self._sync_popup: SyncPopup | None = None
        self._sync_thread: Thread | None = None
        self._sync_fatal_error: str | None = None
        self._completion_celebration: CompletionCelebrationScreen | None = None
        self.offered_field_setups: set[tuple[int, int]] = set()
        self._shutdown_requested = Event()
        self._backend_lock = Lock()
        self._instance_control = InstanceControl(
            lambda: self.post_message(InstanceCloseRequested())
        )

    def save_review_controls(self, controls: ReviewControls) -> None:
        """Persist and activate one complete profile-scoped review keymap."""
        self.preferences.set_review_controls(self.profile, controls)
        self.review_controls = controls
        self.set_keymap(controls.keymap())

    def dispatch_add_on_event(self, event: AddOnEvent) -> tuple[PresentationCue, ...]:
        """Deliver a presentation event without exposing collection operations."""
        report = self.add_ons.dispatch(event)
        for failure in report.failures:
            definition = next(
                definition
                for definition in self.add_ons.definitions
                if definition.id == failure.add_on_id
            )
            self.notify(f"{definition.name} add-on failed.", severity="warning")
        for cue in report.cues:
            self.present_add_on_cue(cue)
        return report.cues

    def present_add_on_cue(self, cue: PresentationCue) -> None:
        """Render one supported cue without exposing application internals."""
        if cue.type is PresentationCueType.NOTICE and cue.message:
            self.notify(cue.message)
        elif cue.type is PresentationCueType.COMPLETION_CELEBRATION:
            values = cue.values or {}
            deck_name = values.get("deck_name", "")
            duration = values.get("duration", "medium")
            celebration = CompletionCelebrationScreen(
                deck_name if isinstance(deck_name, str) else "",
                completion_duration_seconds(
                    duration if isinstance(duration, str) else "medium"
                ),
            )
            self._completion_celebration = celebration
            self.push_screen(celebration)

    def close_completion_celebration(
        self, celebration: CompletionCelebrationScreen
    ) -> None:
        celebration.stop_animation()
        if self._completion_celebration is celebration:
            self._completion_celebration = None
        if self.screen is celebration:
            self.pop_screen()

    def on_mount(self) -> None:
        try:
            self.backend.open()
            self.startup_ready()
        except CollectionInUseError as exc:
            self.push_screen(StartupRecoveryScreen(str(exc)))
        except BackendError as exc:
            self.push_screen(ErrorScreen(str(exc)))

    def startup_ready(self, *, replace: bool = False) -> None:
        if self._shutdown_requested.is_set():
            return
        self._instance_control.start()
        if replace:
            self.switch_screen(DeckScreen())
        else:
            self.push_screen(DeckScreen())

    def on_instance_close_requested(self, message: InstanceCloseRequested) -> None:
        if not self.syncing:
            self.exit()

    def on_unmount(self) -> None:
        self._shutdown_requested.set()
        self._instance_control.close()
        if self._completion_celebration is not None:
            self._completion_celebration.stop_animation()
            self._completion_celebration = None
        with self._backend_lock:
            self.backend.close()

    def action_help(self) -> None:
        if self.syncing or isinstance(self.screen, StartupRecoveryScreen):
            return
        screen = self.screen
        if isinstance(screen, CompletionCelebrationScreen):
            screen.action_skip()
        elif isinstance(screen, SettingsScreen):
            screen.action_back()
        elif isinstance(screen, ReviewScreen) and screen.card is not None:
            self.push_screen(SettingsScreen(screen, initial_tab="sections"))
        else:
            self.push_screen(SettingsScreen(initial_tab="help"))

    def action_quit(self) -> None:
        if isinstance(self.screen, CompletionCelebrationScreen):
            self.screen.action_skip()
        elif not self.syncing:
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

    def start_full_sync(self, direction: FullSyncDirection) -> None:
        if not self.syncing or self._shutdown_requested.is_set():
            return
        self._sync_thread = Thread(
            target=self._sync_in_thread, args=(direction,), name="repetui-full-sync", daemon=True
        )
        self._sync_thread.start()

    def _sync_in_thread(self, direction: FullSyncDirection | None = None) -> None:
        self.post_message(SyncFinished(self._run_sync(direction)))

    def on_sync_finished(self, message: SyncFinished) -> None:
        self._finish_sync(message.result)

    def _run_sync(self, direction: FullSyncDirection | None = None) -> SyncRunResult:
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
                outcome = (
                    self.syncer(self.profile)
                    if direction is None
                    else self.full_syncer(self.profile, direction)
                )
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
        self.syncing = False
        if self.backend.is_open:
            for screen in self.screen_stack:
                if hasattr(screen, "backend_refreshed"):
                    cast(Refreshable, screen).backend_refreshed()
        self._sync_popup = None
        self._sync_origin = None
        self._sync_fatal_error = None
        if fatal and fatal_error is not None:
            self.push_screen(ErrorScreen(f"Could not reopen the Anki collection: {fatal_error}"))
