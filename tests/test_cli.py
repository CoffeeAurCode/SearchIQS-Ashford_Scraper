import socket
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import NetworkBlocked
from searchiqs_scraper.cli import main

REPO = Path(__file__).resolve().parents[1]


def test_module_help_runs():
    result = subprocess.run(
        [sys.executable, "-m", "searchiqs_scraper", "--help"],
        cwd=REPO, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0
    for text in ("--resume RUN_ID", "--export-only RUN_ID", "--local-only", "--publish-incomplete", "exit codes"):
        assert text in result.stdout


@pytest.mark.parametrize(
    "argv",
    [
        ["--resume", "a", "--export-only", "b"],
        ["--local-only", "--export-only", "b"],
        ["--local-only", "--publish-incomplete"],
    ],
)
def test_conflicting_modes_rejected(argv):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2


def test_missing_google_settings_fail_before_scraping(clean_env, capsys):
    assert main([]) == 1
    assert "GOOGLE_SHEET_ID" in capsys.readouterr().err


def test_invalid_config_fails(clean_env, capsys, monkeypatch):
    monkeypatch.setenv("SEARCHIQS_MAX_PAGES", "0")
    assert main(["--local-only"]) == 1
    assert "max_pages" in capsys.readouterr().err


def test_network_is_blocked_in_tests():
    with pytest.raises(NetworkBlocked):
        socket.create_connection(("example.com", 443), timeout=1)
    with pytest.raises(NetworkBlocked):
        socket.socket().connect(("127.0.0.1", 9))
