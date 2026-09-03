import pytest

from repetui.cli import parser


def test_cli_reports_release_0_1_5(capsys) -> None:
    with pytest.raises(SystemExit) as exit_info:
        parser().parse_args(["--version"])

    assert exit_info.value.code == 0
    assert capsys.readouterr().out == "repetui 0.1.5\n"
