from __future__ import annotations

import argparse
import logging
import sys
from typing import Sequence

from . import __version__
from .config import ConfigError, load_config
from .models import Outcome

EPILOG = """\
modes:
  (default)             new run over today-80 days .. today (America/New_York), then publish
  --resume RUN_ID       continue a run with its saved date range, redoing only uncovered windows
  --export-only RUN_ID  publish an existing local run without scraping

exit codes:
  0  scrape COMPLETE and export verified (or --local-only)
  2  scrape INCOMPLETE, or export failed after a successful scrape
  1  FAILED (no access, configuration error, fatal error)

configuration is read from environment variables or a .env file; see .env.example.
"""


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
    log = logging.getLogger("searchiqs_scraper")
    try:
        config = load_config()
        if not args.local_only:
            config.require_sheets()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return Outcome.FAILED.exit_code
    log.debug("effective configuration: %s", config.redacted())

    print("scraping is not implemented yet: the site flow is built after access reconnaissance", file=sys.stderr)
    return Outcome.FAILED.exit_code
