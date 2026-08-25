from repetui.render import html_to_text


def test_renders_blocks_lists_and_ruby_without_browser_noise() -> None:
    html = """
    <style>.card { color: red; }</style>
    <div><b>葬</b><br><ruby>言葉<rt>ことば</rt></ruby></div>
    <ul><li>meaning one</li><li>meaning two</li></ul>
    <script>alert('no')</script>
    """

    rendered = html_to_text(html)

    assert "葬" in rendered
    assert "言葉（ことば）" in rendered
    assert "• meaning one" in rendered
    assert "color" not in rendered
    assert "alert" not in rendered


def test_answer_drops_duplicated_front_side() -> None:
    html = "question<hr id=answer><div>actual answer</div>"

    assert html_to_text(html, answer=True) == "actual answer"


def test_empty_or_media_only_card_has_stable_fallback() -> None:
    assert html_to_text("<style>x</style>[sound:voice.mp3]") == "(empty card)"

