"""Tests for the CLI entry point."""

from typer.testing import CliRunner

import franklinwh_bridge

from franklinwh_bridge.cli import app

runner = CliRunner()


def test_version():
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert franklinwh_bridge.__version__ in result.output


def test_status_no_db(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 1


def test_backup_list_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "backups").mkdir(parents=True)
    result = runner.invoke(app, ["backup", "list"])
    assert result.exit_code == 0
    assert "No backups" in result.output
