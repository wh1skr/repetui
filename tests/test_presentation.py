import pytest

from repetui.presentation import (
    AVReference,
    CardTemplateIdentity,
    RawCardContent,
    SourceField,
    present_card,
)

IDENTITY = CardTemplateIdentity(
    note_type_id=17,
    note_type_name="Japanese",
    template_ordinal=1,
    template_name="Kanji meaning",
)


def raw(front: str, back: str, *fields: SourceField) -> RawCardContent:
    return RawCardContent(IDENTITY, front, back, fields)


@pytest.fixture
def kanji_model_card() -> RawCardContent:
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
    return RawCardContent(
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


def test_preserves_side_secrecy_and_removes_duplicated_front_side() -> None:
    card = present_card(
        raw(
            "<div>葬</div>",
            '<div>葬</div><hr id="answer"><div>そう — burial</div>',
            SourceField("Expression", "葬"),
            SourceField("Mnemonic", "Flowers mark a grave"),
        )
    )

    assert card.identity == IDENTITY
    assert card.front.text == "葬"
    assert "burial" not in card.front.text
    assert "Flowers" not in card.front.text
    assert card.back.text == "そう — burial"
    assert "葬" not in card.back.text


def test_extracts_ordered_stable_sections_from_a_multisection_back() -> None:
    card = present_card(
        raw(
            "葬",
            """葬<hr id=answer>
            <h2>Reading</h2><p>そう</p>
            <h2>Meaning</h2><p>burial; interment</p>
            <h2>Mnemonic</h2><p>Flowers laid upon a grave.</p>
            """,
        )
    )

    assert [section.label for section in card.back.sections] == [
        "Reading",
        "Meaning",
        "Mnemonic",
    ]
    assert [section.id for section in card.back.sections] == [
        "back:heading:reading",
        "back:heading:meaning",
        "back:heading:mnemonic",
    ]
    assert "Flowers laid upon a grave." in card.back.text


def test_extracts_realistic_kanji_label_and_underlined_sections_losslessly(
    kanji_model_card: RawCardContent,
) -> None:
    card = present_card(kanji_model_card)

    assert [section.label for section in card.back.sections] == [
        "Meaning",
        "On'yomi",
        "Kun'yomi",
        "Radicals",
        "Meaning Mnemonic",
        "Reading Mnemonic",
    ]
    assert [section.id for section in card.back.sections] == [
        "back:label:meaning",
        "back:label:on-yomi",
        "back:label:kun-yomi",
        "back:label:radicals",
        "back:heading:meaning-mnemonic",
        "back:heading:reading-mnemonic",
    ]
    assert [section.source_label for section in card.back.sections] == [
        "meaning",
        "on_yomi",
        "kun_yomi",
        "radicals",
        "meaning_mnemonic",
        "reading_mnemonic",
    ]
    assert " ".join(card.back.text.split()) == (
        "Meaning: Stylish On'yomi: すい Kun'yomi: いき "
        "Radicals: 米, 九, 十 (rice, nine, cross) "
        "Meaning Mnemonic mnemonic paragraph meaning info "
        "Reading Mnemonic reading paragraph reading info"
    )
    assert "Kanji Meaning" not in card.back.text


def test_note_fields_can_identify_sections_but_never_supply_display_content() -> None:
    card = present_card(
        raw(
            "<div>葬</div>",
            "葬<hr id=answer><div>そう</div><div>burial</div>",
            SourceField("Reading", "そう"),
            SourceField("Meaning", "burial"),
            SourceField("Hidden", "must never leak"),
        )
    )

    assert [section.source_label for section in card.back.sections] == [
        "Reading",
        "Meaning",
    ]
    assert "must never leak" not in card.front.text + card.back.text


def test_represents_ruby_lists_tables_code_math_typed_answers_and_media() -> None:
    card = present_card(
        raw(
            """
            <ruby>言葉<rt>ことば</rt></ruby>
            <input id="typeans" type="text">
            """,
            """<hr id=answer>
            <ul><li>first</li><li>second</li></ul>
            <table><tr><th>A</th><th>B</th></tr><tr><td>一</td><td>二</td></tr></table>
            <pre><code>if ready:\n    review()</code></pre>
            <anki-mathjax>x^2 + y^2</anki-mathjax>
            <img src="diagram.png" alt="memory diagram">
            [sound:voice.mp3]
            """,
        )
    )

    assert "言葉（ことば）" in card.front.text
    assert "[type answer]" in card.front.text
    assert "• first" in card.back.text
    assert "A │ B" in card.back.text
    assert "if ready:\n    review()" in card.back.text
    assert "[math: x^2 + y^2]" in card.back.text
    assert "[image: memory diagram]" in card.back.text
    assert "[audio: voice.mp3]" in card.back.text


def test_resolves_indexed_anki_av_and_html_source_fallbacks() -> None:
    card = present_card(
        RawCardContent(
            IDENTITY,
            "[anki:play:q:0]",
            (
                "<hr id=answer>[anki:play:a:0] [anki:play:a:1] "
                '<audio><source src="spoken-answer.ogg?cache=1"></audio>'
            ),
            front_av=(AVReference("audio", "front.mp3"),),
            back_av=(
                AVReference("audio", "大人_b.mp3"),
                AVReference("text to speech", "ja_JP"),
            ),
        )
    )

    assert card.front.text == "[audio: front.mp3]"
    assert card.back.text == (
        "[audio: 大人_b.mp3] [text to speech: ja_JP] [audio: spoken-answer.ogg]"
    )


def test_media_only_and_unknown_html_have_complete_deterministic_fallbacks() -> None:
    media = present_card(raw('<img src="map.png">', "<hr id=answer>[sound:a.mp3]"))
    unknown = present_card(
        raw("<mystery>front <unknown>inside</unknown></mystery>", "<odd>back</odd>")
    )

    assert media.front.text == "[image: map.png]"
    assert media.back.text == "[audio: a.mp3]"
    assert unknown.front.text == "front inside"
    assert unknown.back.text == "back"
    assert len(unknown.back.sections) == 1
    assert unknown.back.sections[0].label == "Answer"


def test_hidden_content_stays_hidden_and_empty_back_does_not_repeat_front() -> None:
    card = present_card(
        raw(
            'visible<div style="display: none">front secret</div>',
            'visible<div aria-hidden="true">back secret</div><hr id=answer>',
        )
    )

    assert card.front.text == "visible"
    assert "secret" not in card.front.text + card.back.text
    assert card.back.text == "(empty card)"


def test_hidden_void_elements_do_not_hide_the_content_after_them() -> None:
    card = present_card(raw('<input hidden value="secret">still visible', "answer"))

    assert card.front.text == "still visible"


def test_duplicate_front_removal_keeps_structural_back_sections_without_marker() -> None:
    card = present_card(
        raw(
            "question",
            "question<h2>Meaning</h2><p>answer</p><h2>Extra</h2><p>detail</p>",
        )
    )

    assert [section.label for section in card.back.sections] == ["Meaning", "Extra"]
    assert card.back.text == "Meaning\nanswer\n\nExtra\ndetail"


def test_exact_raw_front_removal_preserves_unlabelled_prelude_sections_and_audio() -> None:
    front = '<div class="word">大人</div><div>Vocabulary</div>'
    card = present_card(
        RawCardContent(
            IDENTITY,
            front,
            front
            + """
            <br>おとな<br>adult [anki:play:a:0]<br><br>
            <u><span><b>Meaning Mnemonic</b></span></u><br>
            a grown person<br><br>
            <u><span><b>Reading Mnemonic</b></span></u><br>
            おとな sounds like adult
            """,
            back_av=(AVReference("audio", "大人_b.mp3"),),
        )
    )

    assert [section.id for section in card.back.sections] == [
        "back:preamble",
        "back:heading:meaning-mnemonic",
        "back:heading:reading-mnemonic",
    ]
    assert " ".join(card.back.text.split()) == (
        "おとな adult [audio: 大人_b.mp3] "
        "Meaning Mnemonic a grown person "
        "Reading Mnemonic おとな sounds like adult"
    )
    assert "大人 Vocabulary" not in " ".join(card.back.text.split())


def test_colon_terminated_underlined_headings_reconcile_as_label_sections() -> None:
    front = '<div class="word">Adult</div>'
    card = present_card(
        RawCardContent(
            CardTemplateIdentity(305, "Vocabulary", 0, "Recognition"),
            front,
            front
            + """
            <br>Adult<br><br>おとな<br><br>
            <u><span><b>Type:</b></span></u><br>Noun<br><br>
            <u><span><b>Kanji:</b></span></u><br>大, 人 (big, person)<br><br>
            <u><span><b>Meaning Explanation</b></span></u><br>a grown person<br><br>
            <u><span><b>Reading Mnemonic</b></span></u><br>おとな sounds grown-up<br><br>
            <u><span><b>Context Example</b></span></u><br>
            Adults study too. [anki:play:a:0]
            """,
            (
                SourceField("type", "Noun"),
                SourceField("kanji", "大, 人 (big, person)"),
                SourceField("meaning_explanation", "a grown person"),
                SourceField("reading_mnemonic", "おとな sounds grown-up"),
                SourceField("context_example", "Adults study too."),
            ),
            back_av=(AVReference("audio", "大人_b.mp3"),),
        )
    )

    assert [section.id for section in card.back.sections] == [
        "back:preamble",
        "back:label:type",
        "back:label:kanji",
        "back:heading:meaning-explanation",
        "back:heading:reading-mnemonic",
        "back:heading:context-example",
    ]
    assert [section.label for section in card.back.sections] == [
        None,
        "Type",
        "Kanji",
        "Meaning Explanation",
        "Reading Mnemonic",
        "Context Example",
    ]
    assert " ".join(card.back.text.split()) == (
        "Adult おとな Type: Noun Kanji: 大, 人 (big, person) "
        "Meaning Explanation a grown person "
        "Reading Mnemonic おとな sounds grown-up "
        "Context Example Adults study too. [audio: 大人_b.mp3]"
    )


def test_cloze_and_long_prose_are_not_truncated() -> None:
    prose = "A detailed explanation " * 40
    card = present_card(
        raw(
            "The capital is <span class=cloze>[...]</span>.",
            f"<hr id=answer>The capital is <span class=cloze>Paris</span>.<p>{prose}</p>",
        )
    )

    assert "[...]" in card.front.text
    assert "Paris" in card.back.text
    assert prose.strip() in card.back.text


def test_reports_terminal_display_width_without_truncating_unicode() -> None:
    card = present_card(raw("葬a\n👩‍💻", "<hr id=answer>そう"))

    assert card.front.display_width == 3
    assert card.front.text == "葬a\n👩‍💻"
