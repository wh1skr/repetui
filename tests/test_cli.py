import pytest

from repetui import __version__
from repetui.cli import parser


def test_cli_reports_package_version(capsys) -> None:
    with pytest.raises(SystemExit) as exit_info:
        parser().parse_args(["--version"])

    assert exit_info.value.code == 0
    assert capsys.readouterr().out == f"repetui {__version__}\n"
