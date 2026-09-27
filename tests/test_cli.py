import socket
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import NetworkBlocked
from fakesite import FakeSite, make_docs
from searchiqs_scraper import cli
from searchiqs_scraper.cli import main
from searchiqs_scraper.config import Config
from searchiqs_scraper.dates import freeze_run_now, task_range
from searchiqs_scraper.http import HttpClient
from searchiqs_scraper.models import Outcome
from test_collector import Runner
from test_sheets import FakeSpreadsheet

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


@pytest.fixture
def fake_site(clean_env, monkeypatch):
    monkeypatch.setenv("SEARCHIQS_OUTPUT_DIR", str(clean_env / "out"))
    docs = make_docs(12, start=task_range(freeze_run_now()).start)
    state = {"egress": "US", "sites": lambda n: FakeSite(docs), "runner": None}

    def pass_runner(config, deadline=None):
        state["runner"] = Runner(state["sites"])
        return state["runner"]

    monkeypatch.setattr(cli, "site_egress_check", lambda config: lambda: state["egress"])
    monkeypatch.setattr(cli, "site_pass_runner", pass_runner)
    monkeypatch.setattr(cli, "site_client",
                        lambda config: HttpClient(state["sites"](1), Config(), sleep=lambda s: None))
    return state


def run_ids(root):
    return sorted(p.name for p in (root / "out").iterdir()) if (root / "out").exists() else []


def test_local_only_run_completes_and_writes_export(fake_site, clean_env, capsys):
    assert cli.main(["--local-only"]) == Outcome.COMPLETE.exit_code
    out = capsys.readouterr().out
    assert "outcome: COMPLETE: 12 records, 0 with issues" in out
    [run_id] = run_ids(clean_env)
    export = clean_env / "out" / run_id / "export"
    assert {p.name for p in export.iterdir()} == {"records.csv", "sheet_rows.json", "report.json"}


def test_challenge_then_resume(fake_site, clean_env, capsys):
    fake_site["sites"] = lambda n: FakeSite(make_docs(1), challenge_on={1})
    assert cli.main(["--local-only"]) == Outcome.FAILED.exit_code
    captured = capsys.readouterr()
    [run_id] = run_ids(clean_env)
    assert "cloudflare-challenge" in captured.err
    assert f"--resume {run_id} after refreshing SEARCHIQS_CF_CLEARANCE" in captured.out

    docs = make_docs(12, start=task_range(freeze_run_now()).start)
    fake_site["sites"] = lambda n: FakeSite(docs)
    assert cli.main(["--local-only", "--resume", run_id]) == Outcome.COMPLETE.exit_code
    assert run_ids(clean_env) == [run_id]


def test_new_run_refuses_non_us_egress(fake_site, clean_env, capsys):
    fake_site["egress"] = "IN"
    assert cli.main(["--local-only"]) == Outcome.FAILED.exit_code
    assert "connect the VPN" in capsys.readouterr().err
    assert run_ids(clean_env) == []


def test_resume_unknown_run_fails(fake_site, capsys):
    assert cli.main(["--local-only", "--resume", "nope"]) == Outcome.FAILED.exit_code
    assert "cannot start" in capsys.readouterr().err


def test_check_access(fake_site, capsys):
    assert cli.main(["--check-access"]) == Outcome.COMPLETE.exit_code
    out = capsys.readouterr().out
    assert "IPv6: egress US, access OK" in out and "SEARCHIQS_IP_FAMILY=6" in out and "IPv4" not in out
    fake_site["sites"] = lambda n: FakeSite(make_docs(1), challenge_on={3})
    assert cli.main(["--check-access"]) == Outcome.FAILED.exit_code
    captured = capsys.readouterr()
    assert "IPv6: egress US, Cloudflare challenge" in captured.out
    assert "IPv4: egress US, Cloudflare challenge" in captured.out
    assert "refresh SEARCHIQS_CF_CLEARANCE" in captured.err


def test_check_access_finds_the_family_the_clearance_was_issued_to(fake_site, monkeypatch, capsys):
    families = []

    def client_for(config):
        families.append(config.ip_family)
        site = FakeSite(make_docs(1), challenge_on={3} if config.ip_family == "6" else set())
        return HttpClient(site, Config(), sleep=lambda s: None)

    monkeypatch.setattr(cli, "site_client", client_for)
    assert cli.main(["--check-access"]) == Outcome.COMPLETE.exit_code
    out = capsys.readouterr().out
    assert families == ["6", "4"]
    assert "IPv6: egress US, Cloudflare challenge" in out and "IPv4: egress US, access OK" in out
    assert "$env:SEARCHIQS_IP_FAMILY = '4'" in out


def test_check_access_with_a_fixed_family_tries_only_that_one(fake_site, monkeypatch, capsys):
    monkeypatch.setenv("SEARCHIQS_IP_FAMILY", "4")
    assert cli.main(["--check-access"]) == Outcome.COMPLETE.exit_code
    out = capsys.readouterr().out
    assert "IPv4: egress US, access OK" in out and "IPv6" not in out and "SEARCHIQS_IP_FAMILY" not in out


@pytest.fixture
def google(clean_env, monkeypatch):
    creds = clean_env / "sa.json"
    creds.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_FILE", str(creds))
    monkeypatch.setenv("GOOGLE_SHEET_ID", "sheet-id")
    sheet = FakeSpreadsheet("Sheet1")
    opened = []
    monkeypatch.setattr(cli, "open_spreadsheet", lambda path, key: opened.append(key) or sheet)
    return sheet, opened


def test_default_run_scrapes_then_publishes(fake_site, google, capsys):
    sheet, opened = google
    assert cli.main([]) == Outcome.COMPLETE.exit_code
    assert opened == ["sheet-id"]
    assert len(sheet.values("Records")) == 13
    assert ["Scrape outcome", "COMPLETE"] in sheet.values("Run Info")
    assert f"published 12 records to {FakeSpreadsheet.url}" in capsys.readouterr().out


def test_export_only_republishes_a_local_run(fake_site, google, clean_env, capsys):
    sheet, opened = google
    assert cli.main(["--local-only"]) == Outcome.COMPLETE.exit_code
    assert opened == []
    [run_id] = run_ids(clean_env)
    assert cli.main(["--export-only", run_id]) == Outcome.COMPLETE.exit_code
    assert opened == ["sheet-id"] and len(sheet.values("Records")) == 13


def test_incomplete_run_is_published_only_on_request(fake_site, google, clean_env, capsys):
    sheet, opened = google
    fake_site["sites"] = lambda n: FakeSite(make_docs(12, start=task_range(freeze_run_now()).start),
                                            ignore_group=True)
    assert cli.main(["--local-only"]) == Outcome.INCOMPLETE.exit_code
    [run_id] = run_ids(clean_env)
    assert cli.main(["--export-only", run_id]) == Outcome.INCOMPLETE.exit_code
    assert opened == [] and "--publish-incomplete" in capsys.readouterr().err
    assert cli.main(["--export-only", run_id, "--publish-incomplete"]) == Outcome.INCOMPLETE.exit_code
    assert opened == ["sheet-id"]
    assert ["Scrape outcome", "INCOMPLETE"] in sheet.values("Run Info")


def test_publish_failure_is_reported_without_losing_the_local_run(fake_site, google, clean_env, monkeypatch, capsys):
    def broken(path, key):
        raise cli.PublishError("spreadsheet not found")

    monkeypatch.setattr(cli, "open_spreadsheet", broken)
    assert cli.main([]) == Outcome.INCOMPLETE.exit_code
    assert "publishing failed: spreadsheet not found" in capsys.readouterr().err
    [run_id] = run_ids(clean_env)
    assert (clean_env / "out" / run_id / "export" / "records.csv").exists()


def test_export_only_unknown_run_fails(google, capsys):
    assert cli.main(["--export-only", "nope"]) == Outcome.FAILED.exit_code
    assert "cannot export nope" in capsys.readouterr().err
