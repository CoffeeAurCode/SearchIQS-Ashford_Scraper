from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import replace
from typing import Sequence

from . import __version__
from .collector import (
    REQUIRED_COUNTRY,
    VERIFIED_COUNT_UNIT,
    Collector,
    site_client,
    site_egress_check,
    site_pass_runner,
)
from .config import Config, ConfigError, load_config
from .dates import freeze_run_now
from .flow import ChallengeError, FlowError, SiteFlow
from .http import HttpError
from .models import PARSER_VERSION, Outcome
from .sheets import PublishError, open_spreadsheet, publish
from .store import (
    ExportData,
    InventoryCorrupt,
    RunLocked,
    RunMeta,
    RunStore,
    StoreError,
    build_export,
    new_run_id,
)

EPILOG = """\
modes:
  (default)             new run over today-80 days .. today (America/New_York), then publish
  --resume RUN_ID       continue a run with its saved date range, redoing only uncovered windows
  --export-only RUN_ID  publish an existing local run without scraping
  --check-access        check US egress and guest access over IPv6, then IPv4 (3 site requests each)

exit codes:
  0  scrape COMPLETE and export verified (or --local-only)
  2  scrape INCOMPLETE, or export failed after a successful scrape
  1  FAILED (no access, configuration error, fatal error)

configuration is read from environment variables or a .env file; see .env.example.
"""

log = logging.getLogger("searchiqs_scraper")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m searchiqs_scraper",
        description="Scrape Ashford, CT (SearchIQS) guest Land Records over plain HTTP "
                    "and publish them to Google Sheets.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--resume", metavar="RUN_ID", help="resume an interrupted run")
    mode.add_argument("--export-only", metavar="RUN_ID", help="publish an existing local run")
    mode.add_argument("--check-access", action="store_true", help="check VPN egress and site access, then exit")
    parser.add_argument("--local-only", action="store_true", help="collect locally and skip Google Sheets")
    parser.add_argument("--publish-incomplete", action="store_true",
                        help="publish an INCOMPLETE run, labelled as such in Run Info")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.local_only and args.export_only:
        parser.error("--local-only cannot be combined with --export-only")
    if args.local_only and args.publish_incomplete:
        parser.error("--local-only cannot be combined with --publish-incomplete")

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        config = load_config()
        if not (args.local_only or args.check_access):
            config.require_sheets()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return Outcome.FAILED.exit_code
    log.debug("effective configuration: %s", config.redacted())

    if args.check_access:
        return check_access(config)
    if args.export_only:
        return export_only(config, args.export_only, args.publish_incomplete)
    code, export, outcome = scrape(config, args.resume)
    if args.local_only or export is None:
        return code
    return publish_export(config, export, outcome, args.publish_incomplete)


def check_access(config: Config) -> int:
    families = ["6", "4"] if config.ip_family == "auto" else [config.ip_family]
    for family in families:
        if _check_family(replace(config, ip_family=family)):
            if config.ip_family == "auto":
                print(f"use this address family for the run: SEARCHIQS_IP_FAMILY={family} "
                      f"(PowerShell: $env:SEARCHIQS_IP_FAMILY = '{family}')")
            print("access OK: guest search page reached")
            return Outcome.COMPLETE.exit_code
    print("no address family got through: the clearance is missing, expired, or was issued to another address. "
          "With the VPN on, reload the site in your browser and refresh SEARCHIQS_CF_CLEARANCE.", file=sys.stderr)
    return Outcome.FAILED.exit_code


def _check_family(config: Config) -> bool:
    label = f"IPv{config.ip_family}"
    try:
        country = site_egress_check(config)()
    except HttpError as exc:
        print(f"{label}: egress check failed: {exc}")
        return False
    if country != REQUIRED_COUNTRY:
        print(f"{label}: egress country is {country}, not {REQUIRED_COUNTRY}: connect the VPN first")
        return False
    client = site_client(config)
    try:
        response = SiteFlow(client).open_search()
    except ChallengeError:
        print(f"{label}: egress {country}, Cloudflare challenge (clearance not valid over this address)")
        return False
    except (FlowError, HttpError) as exc:
        print(f"{label}: egress {country}, site access failed ({exc.code}): {exc}")
        return False
    finally:
        client.close()
    print(f"{label}: egress {country}, access OK (site address {response.remote_ip or 'unknown'})")
    return True


def scrape(config: Config, resume: str | None) -> tuple[int, ExportData | None, Outcome]:
    deadline = time.monotonic() + config.run_deadline
    check_egress = site_egress_check(config)
    try:
        if resume is None:
            country = check_egress()
            if country != REQUIRED_COUNTRY:
                print(f"egress country is {country}, not {REQUIRED_COUNTRY}: connect the VPN first", file=sys.stderr)
                return Outcome.FAILED.exit_code, None, Outcome.FAILED
            run_now = freeze_run_now()
            meta = RunMeta(new_run_id(run_now), run_now, country, VERIFIED_COUNT_UNIT, config.redacted())
            store = RunStore(config.output_dir, meta.run_id)
        else:
            store = RunStore(config.output_dir, resume)
            meta = store.load_run()
            if meta.parser_version != PARSER_VERSION:
                print(f"run {resume} was made with parser version {meta.parser_version}; "
                      f"this version is {PARSER_VERSION}. Start a new run.", file=sys.stderr)
                return Outcome.FAILED.exit_code, None, Outcome.FAILED
    except (HttpError, StoreError) as exc:
        print(f"cannot start: {exc}", file=sys.stderr)
        return Outcome.FAILED.exit_code, None, Outcome.FAILED

    print(f"run {meta.run_id}: {meta.range.start} .. {meta.range.end} (inclusive, America/New_York)")
    try:
        with store.locked(create=resume is None):
            if resume is None:
                store.create(meta)
            collector = Collector(store, meta, site_pass_runner(config, deadline=deadline),
                                  check_egress=check_egress, max_passes=config.max_passes)
            try:
                result = collector.run()
                stopped, stop_code = result.stopped, result.stop_code
            except KeyboardInterrupt:
                stopped, stop_code = "interrupted", "interrupted"
            resolution = store.resolve(meta)
            export = build_export(meta, resolution, stopped)
            store.write_export(export)
    except RunLocked as exc:
        print(f"{exc}: another process is using this run", file=sys.stderr)
        return Outcome.FAILED.exit_code, None, Outcome.FAILED
    except InventoryCorrupt as exc:
        print(f"run inventory is corrupt: {exc}", file=sys.stderr)
        return Outcome.FAILED.exit_code, None, Outcome.FAILED

    outcome = export.outcome
    if stopped and not any(leaf.covered for leaf in resolution.leaves):
        outcome = Outcome.FAILED
    report = export.report
    print(f"outcome: {outcome.value}: {report['records']} records, {report['records_with_issues']} with issues")
    for gap in report["uncovered"]:
        print(f"  uncovered: {gap}")
    if stopped:
        print(f"stopped: {stopped}", file=sys.stderr)
    if outcome is not Outcome.COMPLETE:
        hint = " after refreshing SEARCHIQS_CF_CLEARANCE" if stop_code == "cloudflare-challenge" else ""
        print(f"resume with: python -m searchiqs_scraper --local-only --resume {meta.run_id}{hint}")
    print(f"local export: {store.export_dir}")
    return outcome.exit_code, export, outcome


def export_only(config: Config, run_id: str, publish_incomplete: bool) -> int:
    store = RunStore(config.output_dir, run_id)
    try:
        meta = store.load_run()
        with store.locked():
            resolution = store.resolve(meta)
            export = build_export(meta, resolution)
            store.write_export(export)
    except (StoreError, InventoryCorrupt) as exc:
        print(f"cannot export {run_id}: {exc}", file=sys.stderr)
        return Outcome.FAILED.exit_code
    report = export.report
    print(f"run {run_id}: {export.outcome.value}, {report['records']} records, "
          f"{report['records_with_issues']} with issues")
    for gap in report["uncovered"]:
        print(f"  uncovered: {gap}")
    return publish_export(config, export, export.outcome, publish_incomplete)


def publish_export(config: Config, export: ExportData, outcome: Outcome, publish_incomplete: bool) -> int:
    if outcome is not Outcome.COMPLETE and not publish_incomplete:
        print(f"not publishing a {outcome.value} run (use --publish-incomplete to publish it, labelled as such)",
              file=sys.stderr)
        return outcome.exit_code
    try:
        url = publish(export.tables, open_spreadsheet(config.google_credentials, config.google_sheet_id))
    except PublishError as exc:
        print(f"publishing failed: {exc}", file=sys.stderr)
        return Outcome.INCOMPLETE.exit_code
    except Exception as exc:
        print(f"publishing failed ({type(exc).__name__}): {exc}", file=sys.stderr)
        return Outcome.INCOMPLETE.exit_code
    print(f"published {export.report['records']} records to {url}")
    return outcome.exit_code
