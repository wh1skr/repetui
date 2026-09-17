import pytest

from repetui.presentation import (
    CardTemplateIdentity,
    RawCardContent,
    SourceField,
    TemplateFieldProfile,
    suggest_field_layout,
)

IDENTITY = CardTemplateIdentity(73, "Grammar", 0, "Meaning")


def grammar_card(index: int) -> RawCardContent:
    sentence = f"文{index}[ぶん]を読む。"
    translation = f"Read sentence {index}."
    explanation = f"Explanation {index} is detailed."
    title = f"Pattern {index}"
    structure = f"Noun + pattern {index}"
    question = "What does the whole sentence mean?"
    return RawCardContent(
        IDENTITY,
        f"<ruby>文{index}<rt>ぶん</rt></ruby>を読む。<p>{question}</p>",
        (
            f"<ruby>文{index}<rt>ぶん</rt></ruby>を読む。"
            f"<p>{translation}</p><p>{explanation}</p>"
            f"<p>{title}</p><p>{structure}</p>"
            f"[sound:clip{index}.mp3]<p>context</p>"
        ),
        (
            SourceField("Key", f"1000000000000000000{index}"),
            SourceField("Sentence", sentence),
            SourceField("Answer", f'<span class="focus">{sentence}</span>'),
            SourceField("Question", question),
            SourceField("Translation", translation),
            SourceField("Explanation", explanation),
            SourceField("Title", title),
            SourceField("Structure", structure),
            SourceField("Audio", f"[sound:clip{index}.mp3]"),
            SourceField("Kind", "context"),
            SourceField("SourceURL", f"https://example.test/{index}"),
            SourceField("Optional", ""),
        ),
    )


def test_multi_card_suggestion_maps_visible_fields_and_skips_duplicates() -> None:
    result = suggest_field_layout(tuple(grammar_card(index) for index in range(3)))

    assert result.confidence == "high"
    assert result.profile == TemplateFieldProfile(
        ("Sentence", "Question"),
        ("Translation", "Explanation", "Title", "Structure", "Audio"),
        ("Key", "Answer", "Kind", "SourceURL"),
    )
    assert "Optional" in result.unresolved_fields
    assert result.reasons


def test_single_card_is_not_treated_as_high_confidence() -> None:
    result = suggest_field_layout((grammar_card(1),))

    assert result.confidence == "low"
    assert result.profile is None
    assert "more cards" in " ".join(result.reasons).lower()


def test_repeated_identical_cards_are_not_counted_as_independent_evidence() -> None:
    result = suggest_field_layout((grammar_card(1), grammar_card(1)))

    assert result.confidence == "low"
    assert result.profile is None
    assert "varied" in " ".join(result.reasons).lower()


def test_suggestion_requires_one_stable_template() -> None:
    other = grammar_card(2)
    other = RawCardContent(
        CardTemplateIdentity(74, "Other", 0, "Meaning"),
        other.front_html,
        other.back_html,
        other.fields,
    )

    with pytest.raises(ValueError, match="same template"):
        suggest_field_layout((grammar_card(1), other))


def test_ambiguous_rendered_content_does_not_force_a_layout() -> None:
    samples = tuple(
        RawCardContent(
            IDENTITY,
            f"<div>rendered prompt {index}</div>",
            f"<div>rendered answer {index}</div>",
            (
                SourceField("A", f"unrelated {index}"),
                SourceField("B", f"also unrelated {index}"),
            ),
        )
        for index in range(3)
    )

    result = suggest_field_layout(samples)

    assert result.confidence == "low"
    assert result.profile is None
    assert set(result.unresolved_fields) == {"A", "B"}


def test_many_unresolved_fields_keep_the_suggestion_low_confidence() -> None:
    samples = tuple(
        RawCardContent(
            IDENTITY,
            f"word {index}",
            f"word {index}<hr id=answer>meaning {index}",
            (
                SourceField("Word", f"word {index}"),
                SourceField("Meaning", f"meaning {index}"),
                *(SourceField(f"Opaque {extra}", f"hidden {extra} {index}") for extra in range(5)),
            ),
        )
        for index in range(3)
    )

    result = suggest_field_layout(samples)

    assert result.confidence == "low"
    assert result.profile is None
    assert len(result.unresolved_fields) == 5


def test_duplicate_metadata_field_does_not_beat_visible_word() -> None:
    samples = tuple(
        RawCardContent(
            IDENTITY,
            f"<div>word-{index}</div>",
            f"<div>word-{index}</div><hr id=answer><p>meaning-{index}</p>",
            (
                SourceField("Key", f"word-{index}"),
                SourceField("Word", f"word-{index}"),
                SourceField("Meaning", f"meaning-{index}"),
            ),
        )
        for index in range(3)
    )

    result = suggest_field_layout(samples)

    assert result.confidence == "high"
    assert result.profile == TemplateFieldProfile(
        ("Word",), ("Meaning",), ("Key",)
    )
