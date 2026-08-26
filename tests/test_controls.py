import pytest

from repetui.controls import BindingConflict, ReviewAction, ReviewControls


def test_review_controls_start_with_terminal_defaults() -> None:
    controls = ReviewControls.defaults()

    assert controls.binding(ReviewAction.REVEAL_GOOD) == "enter"
    assert controls.binding(ReviewAction.AGAIN) == "1"
    assert controls.binding(ReviewAction.HARD) == "2"
    assert controls.binding(ReviewAction.GOOD) == "3"
    assert controls.binding(ReviewAction.EASY) == "4"
    assert controls.binding(ReviewAction.UNDO) == "u"
    assert controls.binding(ReviewAction.BURY) == "b"
    assert controls.binding(ReviewAction.SUSPEND) == "x"
    assert controls.binding(ReviewAction.FLAG) == "f"
    assert controls.binding(ReviewAction.SYNC) == "s"


def test_binding_conflict_requires_confirmation_and_unbinds_displaced_action() -> None:
    controls = ReviewControls.defaults()

    with pytest.raises(BindingConflict) as conflict:
        controls.with_binding(ReviewAction.UNDO, "b")

    assert conflict.value.action is ReviewAction.BURY
    assert controls.binding(ReviewAction.UNDO) == "u"
    assert controls.binding(ReviewAction.BURY) == "b"

    replaced = controls.with_binding(ReviewAction.UNDO, "b", replace=True)

    assert replaced.binding(ReviewAction.UNDO) == "b"
    assert replaced.binding(ReviewAction.BURY) is None


def test_selected_action_can_restore_its_default_binding() -> None:
    customized = ReviewControls.defaults().with_binding(ReviewAction.UNDO, "z")

    restored = customized.with_default(ReviewAction.UNDO)

    assert restored.binding(ReviewAction.UNDO) == "u"

