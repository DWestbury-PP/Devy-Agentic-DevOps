"""CLI wiring guard (no network).

`agentic-devops serve` must stay a NAMED subcommand. A single-command Typer app
collapses and drops the name unless a callback is present — which would break
both the documented UX and the container entrypoint.
"""

from typer.testing import CliRunner

from agentic_devops.cli import main

runner = CliRunner()


def test_serve_is_a_named_subcommand():
    result = runner.invoke(main.app, ["serve", "--help"])
    assert result.exit_code == 0
    assert "Start the proxy service" in result.output
