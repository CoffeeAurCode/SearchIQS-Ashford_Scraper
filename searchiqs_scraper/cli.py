from __future__ import annotations

import argparse
import logging
import sys
import time
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
from .store import InventoryCorrupt, RunLocked, RunMeta, RunStore, StoreError, build_export, new_run_id

EPILOG = """\
modes:
  (default)             new run over today-80 days .. today (America/New_York), then publish
  --resume RUN_ID       continue a run with its saved date range, redoing only uncovered windows
  --export-only RUN_ID  publish an existing local run without scraping
  --check-access        check US egress and that the guest search page loads (3 site requests)

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
    if not args.local_only:
        print("Google Sheets publishing is not implemented yet; use --local-only", file=sys.stderr)
        return Outcome.FAILED.exit_code
    return scrape(config, args.resume)


def check_access(config: Config) -> int:
    try:
        country = site_egress_check(config)()
    except HttpError as exc:
        print(f"egress check failed: {exc}", file=sys.stderr)
        return Outcome.FAILED.exit_code
    print(f"egress country: {country}")
    if country != REQUIRED_COUNTRY:
        print(f"not {REQUIRED_COUNTRY}: connect the VPN first", file=sys.stderr)
        return Outcome.FAILED.exit_code
    client = site_client(config)
    try:
        SiteFlow(client).open_search()
    except ChallengeError as exc:
        print(str(exc), file=sys.stderr)
        return Outcome.FAILED.exit_code
    except (FlowError, HttpError) as exc:
        print(f"site access failed ({exc.code}): {exc}", file=sys.stderr)
        return Outcome.FAILED.exit_code
    finally:
        client.close()
    print("access OK: guest search page reached")
    return Outcome.COMPLETE.exit_code


def scrape(config: Config, resume: str | None) -> int:
    deadline = time.monotonic() + config.run_deadline
    check_egress = site_egress_check(config)
    try:
        if resume is None:
            country = check_egress()
            if country != REQUIRED_COUNTRY:
                print(f"egress country is {country}, not {REQUIRED_COUNTRY}: connect the VPN first", file=sys.stderr)
                return Outcome.FAILED.exit_code
            run_now = freeze_run_now()
            meta = RunMeta(new_run_id(run_now), run_now, country, VERIFIED_COUNT_UNIT, config.redacted())
            store = RunStore(config.output_dir, meta.run_id)
        else:
            store = RunStore(config.output_dir, resume)
            meta = store.load_run()
            if meta.parser_version != PARSER_VERSION:
                print(f"run {resume} was made with parser version {meta.parser_version}; "
                      f"this version is {PARSER_VERSION}. Start a new run.", file=sys.stderr)
                return Outcome.FAILED.exit_code
    except (HttpError, StoreError) as exc:
        print(f"cannot start: {exc}", file=sys.stderr)
        return Outcome.FAILED.exit_code

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
        return Outcome.FAILED.exit_code
    except InventoryCorrupt as exc:
        print(f"run inventory is corrupt: {exc}", file=sys.stderr)
        return Outcome.FAILED.exit_code

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
    return outcome.exit_code
