from types import SimpleNamespace

from repetui.backend import AnkiBackend, DueCounts


class FakeRendered:
    question_text = "<div>front</div>"
    answer_text = "front<hr id=answer><div>back [anki:play:a:0] [anki:play:a:1]</div>"
    question_av_tags: list[object] = []
    answer_av_tags: list[object] = [
        SimpleNamespace(filename="大人_b.mp3"),
        SimpleNamespace(lang="ja_JP", field_text="大人"),
    ]

    def question_and_style(self) -> str:
        return self.question_text

    def answer_and_style(self) -> str:
        return self.answer_text


class FakeNote:
    def items(self) -> list[tuple[str, str]]:
        return [("Front", "front"), ("Back", "back")]


class FakeCard:
    id = 99
    ord = 0

    def __init__(self) -> None:
        self.timer_started = False

    def render_output(self) -> FakeRendered:
        return FakeRendered()

    def note_type(self) -> dict[str, object]:
        return {"id": 123, "name": "Basic"}

    def template(self) -> dict[str, object]:
        return {"name": "Card 1"}

    def note(self) -> FakeNote:
        return FakeNote()

    def start_timer(self) -> None:
        self.timer_started = True


class FakeScheduler:
    def __init__(self) -> None:
        child = SimpleNamespace(
            name="Languages::Japanese",
            deck_id=2,
            new_count=3,
            learn_count=4,
            review_count=5,
            children=[],
        )
        parent = SimpleNamespace(
            name="Languages",
            deck_id=1,
            new_count=3,
            learn_count=4,
            review_count=5,
            children=[child],
        )
        self.tree = SimpleNamespace(name="", children=[parent])
        self.fake_card = FakeCard()
        self.answered = None
        self.queue_calls = 0

    def deck_due_tree(self):
        return self.tree

    def get_queued_cards(self, fetch_limit: int):
        assert fetch_limit == 1
        self.queue_calls += 1
        if self.queue_calls > 1:
            return SimpleNamespace(cards=[])
        queued = SimpleNamespace(card=SimpleNamespace(id=99), states="states")
        return SimpleNamespace(cards=[queued])

    def build_answer(self, *, card, states, rating):
        return SimpleNamespace(card=card, states=states, rating=rating)

    def answer_card(self, answer) -> None:
        self.answered = answer


class FakeDecks:
    def __init__(self) -> None:
        self.selected = None

    def name(self, deck_id: int) -> str:
        return {1: "Languages", 2: "Languages::Japanese"}[deck_id]

    def select(self, deck_id: int) -> None:
        self.selected = deck_id


class FakeCollection:
    def __init__(self) -> None:
        self.sched = FakeScheduler()
        self.decks = FakeDecks()

    def get_card(self, card_id: int) -> FakeCard:
        assert card_id == 99
        return self.sched.fake_card


def backend() -> tuple[AnkiBackend, FakeCollection]:
    service = AnkiBackend.__new__(AnkiBackend)
    service.collection_path = None
    collection = FakeCollection()
    service._collection = collection
    service._deck_id = None
    service._current = None
    return service, collection


def test_flattens_nested_decks_with_aggregate_parent_counts() -> None:
    service, _ = backend()

    decks = service.decks()

    assert [(deck.name, deck.depth) for deck in decks] == [
        ("Languages", 0),
        ("Languages::Japanese", 1),
    ]
    assert decks[0].counts == DueCounts(new=3, learning=4, review=5)
    assert decks[0].counts.total == 12
    assert decks[1].counts == DueCounts(new=3, learning=4, review=5)
    assert decks[1].counts.total == 12


def test_review_uses_anki_rendering_and_scheduler() -> None:
    service, collection = backend()
    service.begin_review(2)

    card = service.next_card()

    assert card is not None
    assert card.presentation.front.text == "front"
    assert card.presentation.back.text == ("back [audio: 大人_b.mp3] [text to speech: ja_JP]")
    assert card.identity.note_type_id == 123
    assert card.identity.note_type_name == "Basic"
    assert card.identity.template_ordinal == 0
    assert card.identity.template_name == "Card 1"

    service.answer(3)

    assert collection.sched.fake_card.timer_started is True
    assert collection.sched.answered is not None
    assert service.next_card() is None
