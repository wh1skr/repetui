import pytest

from repetui.backend import DueCounts, ReviewQueue
from repetui.controls import ReviewAction, ReviewControls
from repetui.flow import SectionState, compose_ratings, compose_review
from repetui.preferences import SectionMode
from repetui.presentation import (
    CardTemplateIdentity,
    RawCardContent,
    SourceField,
    present_card,
)


def real_kanji_presentation():
    front = (
        '<div class="kanji">粋</div>'
        '<div class="quest">Kanji <b>Meaning</b></div>'
        '<div class="input">[[type:sort_id]]</div>'
    )
    back_only = """
    <br><span><b>Meaning:</b> Stylish</span><br>
    <span><b>On&apos;yomi:</b></span> すい
    <span><b>Kun&apos;yomi:</b></span> いき<br>
    <span><b>Radicals:</b></span> 米, 九, 十 (rice, nine, cross)<br><br>
    <u><span><b>Meaning Mnemonic</b></span></u><br><br>
    <span>mnemonic paragraph<br><br><i>meaning info</i></span><br><br>
    <u><span><b>Reading Mnemonic</b></span></u><br><br>
    <span>reading paragraph<br><br><i>reading info</i></span>
    """
    return present_card(
        RawCardContent(
            CardTemplateIdentity(204, "kanji Model", 0, "Recognition"),
            front,
            front + back_only,
            (
                SourceField("meaning", "Stylish"),
                SourceField("on_yomi", "すい"),
                SourceField("kun_yomi", "いき"),
                SourceField("radicals", "米, 九, 十 (rice, nine, cross)"),
                SourceField("meaning_mnemonic", "mnemonic paragraph"),
                SourceField("reading_mnemonic", "reading paragraph"),
            ),
        )
    )


def test_real_kanji_card_becomes_compact_flow_without_control_noise() -> None:
    presentation = real_kanji_presentation()
    states = tuple(
        SectionState(section, SectionMode.SHOW)
        for section in presentation.back.sections
    )

    front = compose_review(
        presentation,
        "完全な統計::日本語",
        DueCounts(8, 17, 213),
        40,
        revealed=False,
    ).plain
    revealed = compose_review(
        presentation,
        "完全な統計::日本語",
        DueCounts(8, 17, 213),
        40,
        revealed=True,
        sections=states,
    ).plain

    assert front == "粋 · Kanji Meaning          238 8/17/213"
    assert "[type answer]" not in front
    assert "Recognition" not in front
    assert "\n" not in front
    assert revealed.splitlines()[1] == (
        "Meaning: Stylish  ·  On'yomi: すい  ·  Kun'yomi: いき  ·  "
        "Radicals: 米, 九, 十 (rice, nine, cross)"
    )
    assert "cross)\nMeaning Mnemonic · mnemonic paragraph" in revealed
    assert "meaning info\nReading Mnemonic · reading paragraph" in revealed
    assert "reading info" in revealed


def test_review_count_cluster_keeps_neutral_total_and_anki_split_colours() -> None:
    result = compose_review(
        real_kanji_presentation(),
        "Japanese",
        DueCounts(9, 5, 114),
        40,
        revealed=False,
    )
    styled_fragments = {
        (result.plain[span.start : span.end], span.style) for span in result.spans
    }

    assert result.plain.endswith("128 9/5/114")
    assert ("128", "#aaa49b") in styled_fragments
    assert ("9", "#68a8df") in styled_fragments
    assert ("5", "#dc6b72") in styled_fragments
    assert ("114", "#79c98b") in styled_fragments


@pytest.mark.parametrize(
    ("queue", "underlined_count"),
    [
        (ReviewQueue.NEW, "9"),
        (ReviewQueue.LEARNING, "5"),
        (ReviewQueue.REVIEW, "114"),
    ],
)
def test_current_queue_underlines_only_its_count(
    queue: ReviewQueue, underlined_count: str
) -> None:
    result = compose_review(
        real_kanji_presentation(),
        "Japanese",
        DueCounts(9, 5, 114),
        40,
        revealed=False,
        current_queue=queue,
    )
    underlined = [
        result.plain[span.start : span.end]
        for span in result.spans
        if "underline" in str(span.style)
    ]

    assert underlined == [underlined_count]


def test_expanded_inline_label_does_not_repeat_its_heading() -> None:
    presentation = real_kanji_presentation()
    meaning = presentation.back.sections[0]

    revealed = compose_review(
        presentation,
        "Japanese",
        DueCounts(1, 0, 0),
        40,
        revealed=True,
        sections=(
            SectionState(
                meaning,
                SectionMode.FOLD,
                expanded=True,
                selected=True,
            ),
        ),
    ).plain

    assert "▾ Meaning\nStylish" in revealed
    assert "▾ Meaning\nMeaning:" not in revealed


def test_rating_row_uses_current_bindings_and_marks_unbound_actions() -> None:
    controls = ReviewControls.defaults().with_binding(
        ReviewAction.AGAIN, "2", replace=True
    )

    result = compose_ratings(40, controls)

    assert result.plain == "2 again  - hard  3 good  4 easy"
