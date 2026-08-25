"""Narrow application-facing wrapper around Anki's native scheduler."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .presentation import (
    AVReference,
    CardPresentation,
    CardTemplateIdentity,
    RawCardContent,
    SourceField,
    present_card,
)


class BackendError(RuntimeError):
    """Raised when the local Anki collection cannot be used."""


def _av_references(tags: list[Any]) -> tuple[AVReference, ...]:
    references: list[AVReference] = []
    for tag in tags:
        if filename := getattr(tag, "filename", None):
            name = str(filename).replace("\\", "/").rsplit("/", 1)[-1]
            references.append(AVReference("audio", name))
        else:
            language = str(getattr(tag, "lang", "")).strip()
            references.append(AVReference("text to speech", language or None))
    return tuple(references)


@dataclass(frozen=True)
class DueCounts:
    new: int
    learning: int
    review: int

    @property
    def total(self) -> int:
        return self.new + self.learning + self.review


@dataclass(frozen=True)
class Deck:
    id: int
    name: str
    depth: int
    counts: DueCounts

    @property
    def leaf_name(self) -> str:
        return self.name.rsplit("::", 1)[-1]


@dataclass(frozen=True)
class ReviewCard:
    id: int
    presentation: CardPresentation

    @property
    def identity(self) -> CardTemplateIdentity:
        return self.presentation.identity


class AnkiBackend:
    """Own one collection and expose only the operations repetui needs."""

    def __init__(self, collection_path: Path) -> None:
        self.collection_path = collection_path
        self._collection: Any | None = None
        self._deck_id: int | None = None
        self._current: tuple[Any, Any] | None = None

    @property
    def is_open(self) -> bool:
        return self._collection is not None

    def open(self) -> None:
        if self.is_open:
            return
        try:
            from anki.collection import Collection

            self._collection = Collection(str(self.collection_path))
        except Exception as exc:
            message = str(exc)
            if "lock" in message.lower() or "anki already open" in message.lower():
                raise BackendError("Close Anki Desktop before starting repetui.") from exc
            raise BackendError(f"Could not open the Anki collection: {message}") from exc

    def close(self) -> None:
        collection, self._collection = self._collection, None
        self._current = None
        if collection is not None:
            with contextlib.suppress(Exception):
                collection.close()

    def _require_collection(self) -> Any:
        if self._collection is None:
            raise BackendError("The Anki collection is not open.")
        return self._collection

    def decks(self) -> list[Deck]:
        collection = self._require_collection()
        root = collection.sched.deck_due_tree()
        result: list[Deck] = []

        def visit(node: Any, depth: int) -> None:
            if node.name:
                full_name = collection.decks.name(node.deck_id)
                result.append(
                    Deck(
                        id=node.deck_id,
                        name=full_name,
                        depth=depth,
                        counts=DueCounts(node.new_count, node.learn_count, node.review_count),
                    )
                )
                depth += 1
            for child in node.children:
                visit(child, depth)

        visit(root, 0)
        return result

    def begin_review(self, deck_id: int) -> None:
        collection = self._require_collection()
        if not any(deck.id == deck_id for deck in self.decks()):
            raise BackendError(f"Deck no longer exists: {deck_id}")
        collection.decks.select(deck_id)
        self._deck_id = deck_id
        self._current = None

    def counts(self) -> DueCounts:
        if self._deck_id is None:
            return DueCounts(0, 0, 0)
        for deck in self.decks():
            if deck.id == self._deck_id:
                return deck.counts
        return DueCounts(0, 0, 0)

    def next_card(self) -> ReviewCard | None:
        collection = self._require_collection()
        if self._deck_id is None:
            raise BackendError("Choose a deck before requesting a card.")
        collection.decks.select(self._deck_id)
        queued = collection.sched.get_queued_cards(fetch_limit=1)
        if not queued.cards:
            self._current = None
            return None

        queued_card = queued.cards[0]
        card = collection.get_card(queued_card.card.id)
        rendered = card.render_output()
        note_type = card.note_type()
        template = card.template()
        note = card.note()
        raw_content = RawCardContent(
            identity=CardTemplateIdentity(
                note_type_id=int(note_type["id"]),
                note_type_name=str(note_type["name"]),
                template_ordinal=int(card.ord),
                template_name=str(template["name"]),
            ),
            front_html=rendered.question_text,
            back_html=rendered.answer_text,
            fields=tuple(SourceField(name, html) for name, html in note.items()),
            front_av=_av_references(rendered.question_av_tags),
            back_av=_av_references(rendered.answer_av_tags),
        )
        self._current = (card, queued_card.states)
        return ReviewCard(id=card.id, presentation=present_card(raw_content))

    def answer(self, rating: int) -> None:
        collection = self._require_collection()
        if self._current is None:
            raise BackendError("There is no current card to answer.")
        if rating not in {1, 2, 3, 4}:
            raise ValueError("Rating must be between 1 and 4.")

        # Importing Collection first completes Anki's scheduler package setup.
        # Importing scheduler.v3 directly in a lightweight/test process can
        # otherwise encounter Anki's internal circular imports.
        from anki.collection import Collection as _Collection  # noqa: F401
        from anki.scheduler.v3 import CardAnswer

        card, states = self._current
        rating_map = {
            1: CardAnswer.Rating.AGAIN,
            2: CardAnswer.Rating.HARD,
            3: CardAnswer.Rating.GOOD,
            4: CardAnswer.Rating.EASY,
        }
        card.start_timer()
        answer = collection.sched.build_answer(
            card=card,
            states=states,
            rating=rating_map[rating],
        )
        collection.sched.answer_card(answer)
        self._current = None
