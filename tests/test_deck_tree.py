from repetui.backend import Deck, DueCounts
from repetui.deck_tree import visible_deck_rows


def deck(deck_id: int, name: str, depth: int) -> Deck:
    return Deck(deck_id, name, depth, DueCounts(1, 2, 3))


DECKS = [
    deck(1, "日本語", 0),
    deck(2, "日本語::漢字", 1),
    deck(3, "日本語::漢字::N5", 2),
    deck(4, "日本語::語彙", 1),
    deck(5, "AWS", 0),
    deck(6, "AWS::SAA-C03", 1),
]


def test_first_run_shows_only_collapsed_top_level_parents() -> None:
    rows = visible_deck_rows(DECKS, expanded_deck_ids=set())

    assert [row.deck.id for row in rows] == [1, 5]
    assert [(row.is_parent, row.expanded) for row in rows] == [
        (True, False),
        (True, False),
    ]


def test_expanding_nested_parents_reveals_only_their_visible_subtrees() -> None:
    parent_open = visible_deck_rows(DECKS, expanded_deck_ids={1})
    nested_open = visible_deck_rows(DECKS, expanded_deck_ids={1, 2})

    assert [row.deck.id for row in parent_open] == [1, 2, 4, 5]
    assert [(row.deck.id, row.is_parent, row.expanded) for row in parent_open] == [
        (1, True, True),
        (2, True, False),
        (4, False, False),
        (5, True, False),
    ]
    assert [row.deck.id for row in nested_open] == [1, 2, 3, 4, 5]


def test_saved_ids_for_deleted_decks_are_ignored() -> None:
    rows = visible_deck_rows(DECKS, expanded_deck_ids={999})

    assert [row.deck.id for row in rows] == [1, 5]
