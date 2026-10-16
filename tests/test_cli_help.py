"""Every CLI command must render --help.

typer and click are tightly coupled: a click upgrade (spaCy now requires click>=8.2.1) broke
`--help` for every command with "make_metavar() missing 1 required positional argument",
and no other test noticed because none of them render help. This does.
"""
import pytest
from typer.testing import CliRunner

from app.cli import app

runner = CliRunner()
COMMANDS = sorted(c.name or c.callback.__name__.replace("_", "-") for c in app.registered_commands)


def test_top_level_help_renders():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    assert "Usage" in result.output


@pytest.mark.parametrize("command", COMMANDS)
def test_each_command_help_renders(command):
    result = runner.invoke(app, [command, "--help"])
    assert result.exit_code == 0, f"{command}: {result.output[-400:]}"
