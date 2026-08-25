from repetui.backend import DueCounts
from repetui.flow import SectionState, compose_review
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

    assert front == "粋 · Kanji Meaning         238  8/17/213"
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
