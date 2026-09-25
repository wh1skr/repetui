from repetui.card_text import (
    annotated,
    annotations,
    extract_readings,
    inline_readings,
    normalise,
)


def test_normalization_preserves_source_occurrence_and_partial_word_mark():
    text = annotated("  catcat  cat  ", ((5, 8), (10, 13)))
    result = normalise(text)
    assert result.plain == "catcat cat"
    assert annotations(result)[0] == ((3, 6), (7, 10))


def test_normalization_keeps_annotation_on_replaced_media_placeholder():
    source = "[sound:dir/a.mp3] 猫"
    result = normalise(annotated(source, ((0, 17),), ((18, 19, "ねこ"),)))
    assert result.plain == "[audio: a.mp3] 猫"
    assert annotations(result)[0] == ((0, 14),)
    assert inline_readings(result).plain == "[audio: a.mp3] 猫[ねこ]"


def test_bracket_removal_and_slicing_keep_marks_on_the_correct_repeated_word():
    text = "猫[ねこ]と猫[びょう]"
    result = extract_readings(annotated(text, ((6, len(text)),)))
    assert result.plain == "猫と猫"
    assert annotations(result) == (((2, 3),), ((0, 1, "ねこ"), (2, 3, "びょう")))
    assert inline_readings(result[2:]).plain == "猫[びょう]"


def test_ruby_reading_survives_whitespace_normalization_as_one_annotation():
    result = normalise(annotated(" a  b ", readings=((1, 5, "reading"),)))
    assert result.plain == "a b"
    assert inline_readings(result).plain == "a b[reading]"


def test_adjacent_identical_readings_remain_two_annotations():
    result = normalise(annotated("猫猫", readings=((0, 1, "ねこ"), (1, 2, "ねこ"))))
    assert inline_readings(result).plain == "猫[ねこ]猫[ねこ]"


def test_joining_independent_identical_texts_does_not_merge_readings():
    first = annotated("猫", readings=((0, 1, "ねこ"),))
    second = annotated("猫", readings=((0, 1, "ねこ"),))
    assert inline_readings(normalise(first + second)).plain == "猫[ねこ]猫[ねこ]"


def test_multiline_ruby_and_code_indentation_survive_normalization():
    result = normalise(annotated(" a \n b ", readings=((1, 6, "reading"),)))
    assert inline_readings(result).plain == "a\nb[reading]"
    code = normalise(annotated("\n```text\n  a   b  \n\n\n  c\n```\n"))
    assert code.plain == "```text\n  a   b\n\n  c\n```"


def test_raw_control_breaks_are_normalized_before_rich_strips_them():
    raw = "first\r\nsecond\v猫\fend"
    offset = raw.index("猫")
    result = normalise(annotated(raw, ((offset, offset + 1),), ((offset, offset + 1, "ねこ"),)))
    assert result.plain == "first\nsecond\n猫\nend"
    assert annotations(result)[0] == ((13, 14),)
    assert inline_readings(result).plain == "first\nsecond\n猫[ねこ]\nend"
