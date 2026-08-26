"""Pure review-action key binding rules."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ReviewAction(str, Enum):
    REVEAL_GOOD = "reveal_good"
    AGAIN = "again"
    HARD = "hard"
    GOOD = "good"
    EASY = "easy"
    UNDO = "undo"
    BURY = "bury"
    SUSPEND = "suspend"
    FLAG = "flag"
    SYNC = "sync"

    @property
    def label(self) -> str:
        return {
            ReviewAction.REVEAL_GOOD: "Reveal / Good",
            ReviewAction.AGAIN: "Again",
            ReviewAction.HARD: "Hard",
            ReviewAction.GOOD: "Good",
            ReviewAction.EASY: "Easy",
            ReviewAction.UNDO: "Undo",
            ReviewAction.BURY: "Bury",
            ReviewAction.SUSPEND: "Suspend",
            ReviewAction.FLAG: "Flag",
            ReviewAction.SYNC: "Sync",
        }[self]


DEFAULT_REVIEW_BINDINGS: dict[ReviewAction, str] = {
    ReviewAction.REVEAL_GOOD: "enter",
    ReviewAction.AGAIN: "1",
    ReviewAction.HARD: "2",
    ReviewAction.GOOD: "3",
    ReviewAction.EASY: "4",
    ReviewAction.UNDO: "u",
    ReviewAction.BURY: "b",
    ReviewAction.SUSPEND: "x",
    ReviewAction.FLAG: "f",
    ReviewAction.SYNC: "s",
}

FIXED_REVIEW_KEYS = frozenset(
    {"j", "k", "g", "G", "escape", "q", "?", "question_mark", "space"}
)
SUPPORTED_NAMED_KEYS = frozenset({"enter", "tab"})
UNBOUND_KEYMAP_SENTINEL = "unbound"


def is_supported_control_key(key: str) -> bool:
    """Return whether a captured key can safely own a review action."""
    if key in FIXED_REVIEW_KEYS:
        return False
    if key in SUPPORTED_NAMED_KEYS:
        return True
    return len(key) == 1 and key.isprintable() and key != ","


class BindingConflict(ValueError):
    """Raised when a requested key belongs to another review action."""

    def __init__(self, action: ReviewAction) -> None:
        super().__init__(f"Key is already bound to {action.value}.")
        self.action = action


@dataclass(frozen=True)
class ReviewControls:
    """One complete, conflict-free review-action mapping."""

    _bindings: tuple[tuple[ReviewAction, str | None], ...]

    @classmethod
    def defaults(cls) -> ReviewControls:
        return cls(tuple(DEFAULT_REVIEW_BINDINGS.items()))

    @classmethod
    def from_saved(cls, saved: object) -> ReviewControls:
        if not isinstance(saved, dict):
            return cls.defaults()
        bindings: dict[ReviewAction, str | None] = dict(DEFAULT_REVIEW_BINDINGS)
        for action_name, key in saved.items():
            try:
                action = ReviewAction(action_name)
            except (TypeError, ValueError):
                return cls.defaults()
            if key is not None and (
                not isinstance(key, str) or not is_supported_control_key(key)
            ):
                return cls.defaults()
            bindings[action] = key
        assigned = [key for key in bindings.values() if key is not None]
        if len(assigned) != len(set(assigned)):
            return cls.defaults()
        return cls(tuple((action, bindings[action]) for action in ReviewAction))

    def binding(self, action: ReviewAction) -> str | None:
        return dict(self._bindings)[action]

    def saved_overrides(self) -> dict[str, str | None]:
        return {
            action.value: key
            for action, key in self._bindings
            if key != DEFAULT_REVIEW_BINDINGS[action]
        }

    def keymap(self) -> dict[str, str]:
        return {
            f"review.{action.value}": key or UNBOUND_KEYMAP_SENTINEL
            for action, key in self._bindings
        }

    def with_default(
        self, action: ReviewAction, *, replace: bool = False
    ) -> ReviewControls:
        return self.with_binding(
            action,
            DEFAULT_REVIEW_BINDINGS[action],
            replace=replace,
        )

    def with_binding(
        self,
        action: ReviewAction,
        key: str,
        *,
        replace: bool = False,
    ) -> ReviewControls:
        if not is_supported_control_key(key):
            raise ValueError(f"Unsupported review control key: {key}")
        bindings = dict(self._bindings)
        conflict = next(
            (
                bound_action
                for bound_action, bound_key in bindings.items()
                if bound_action is not action and bound_key == key
            ),
            None,
        )
        if conflict is not None and not replace:
            raise BindingConflict(conflict)
        if conflict is not None:
            bindings[conflict] = None
        bindings[action] = key
        return ReviewControls(tuple(bindings.items()))

