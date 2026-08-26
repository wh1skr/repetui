"""Pure visibility model for the compact deck tree."""

from __future__ import annotations

from dataclasses import dataclass

from .backend import Deck


@dataclass(frozen=True)
class VisibleDeckRow:
    """One deck row annotated with the state needed by the menu."""

    deck: Deck
    is_parent: bool
    expanded: bool


def visible_deck_rows(
    decks: list[Deck], expanded_deck_ids: set[int] | frozenset[int]
) -> tuple[VisibleDeckRow, ...]:
    """Return the preorder rows whose full ancestor chain is expanded."""
    rows: list[VisibleDeckRow] = []
    expanded_ancestors: list[bool] = []

    for index, deck in enumerate(decks):
        del expanded_ancestors[deck.depth :]
        visible = all(expanded_ancestors)
        is_parent = index + 1 < len(decks) and decks[index + 1].depth > deck.depth
        expanded = is_parent and deck.id in expanded_deck_ids

        if visible:
            rows.append(VisibleDeckRow(deck, is_parent, expanded))
        if is_parent:
            expanded_ancestors.append(expanded)

    return tuple(rows)
