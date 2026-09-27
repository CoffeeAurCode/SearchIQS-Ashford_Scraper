# SearchIQS Ashford, CT Land Records Scraper

Scrapes **Land Records** from the [SearchIQS Ashford, CT](https://www.searchiqs.com/CTASH/) guest search for a
dynamic date range (**today − 80 days → today**, America/New_York), follows every result page, and exports all
records to a **Google Sheet**.

Plain HTTP only: `curl_cffi` + BeautifulSoup replay the site's ASP.NET WebForms postbacks. No Selenium,
Playwright, Puppeteer, or any other browser automation.

**Sample output:** [Google Sheet (read-only)](SAMPLE_SHEET_URL). See [Status and limitations](#status-and-limitations)
for how it was produced.

## What it does

1. Opens the site and clicks **Search Records as Guest** (the `btnGuestLogin` postback).
2. Selects **Land Records** under Document Group (AutoPostBack) and checks the selection stuck.
3. Sets **From** = today − 80 days and **To** = today, computed at run start (never hard-coded).
4. Searches and follows **Next** through every page. Each page's criteria echo, page number and total are checked.
5. Extracts **Party 1, Party 2, Type, Book-Page, Date, Description, Additional Description, Related** for every row
   (plus `Doc ID` and `Issues` columns).
6. Exports to a Google Sheet (`Records` and `Run Info` tabs; an `Overflow` tab only if a cell exceeds Sheets' limit).

Completeness is checked, not assumed:

- Every search window is fetched **twice** in fresh sessions. The window is COMPLETE only if both passes return the
  same total and the same set of rows, and the row count matches the site's "N documents found".
- If fewer rows come back than the site reports (a result cap) or the page limit is hit, the date range is split in half
  and each half is fetched separately.
- Progress is saved per window, so an interrupted run can be resumed without refetching finished windows.

## Requirements

- Python 3.11+
- A **US VPN**. The site blocks other countries.
- A **Cloudflare clearance cookie** from your own browser. The search pages sit behind a Cloudflare check that
  plain HTTP can't pass. You pass it once in a normal browser, and the scraper reuses the cookie (see below).
- For the Sheets export: a Google Cloud **service account** key and a spreadsheet shared with it.

## Setup

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # Windows: copy .env.example .env
```

All settings live in `.env` (or real environment variables, which take precedence). See `.env.example`.

### Google Sheets

1. In [Google Cloud Console](https://console.cloud.google.com/), create a project, enable the **Google Sheets API**,
   create a **service account**, and download a JSON key.
2. Save the key as `credentials/service-account.json` (ignored by git).
3. Create a Google Sheet. **Share** it with the service account's email (`...@...iam.gserviceaccount.com`) as
   **Editor**. For a read-only public link, set General access to **Anyone with the link → Viewer**.
4. In `.env`, set `GOOGLE_SERVICE_ACCOUNT_FILE=credentials/service-account.json` and `GOOGLE_SHEET_ID=` the ID from
   the sheet URL (`https://docs.google.com/spreadsheets/d/<ID>/edit`).

### Cloudflare clearance (each session)

With the **VPN connected**:

1. In Chrome, open `https://www.searchiqs.com/CTASH/`, complete the "Verify you are human" check if shown, and click
   **Search Records as Guest**.
2. DevTools (F12) → Application → Cookies → `https://www.searchiqs.com` → copy the value of `cf_clearance` into
   `SEARCHIQS_CF_CLEARANCE`.
3. In the DevTools Console, run `navigator.userAgent` and copy the result into `SEARCHIQS_USER_AGENT`.

The cookie is tied to your IP address and User-Agent, so the scraper must run on the same machine and VPN connection.
`SEARCHIQS_IP_FAMILY` (`auto`, `4`, `6`) pins IPv4 or IPv6, and `--check-access` tells you which one works. If your
antivirus intercepts HTTPS, point `SEARCHIQS_CA_BUNDLE` at a PEM bundle that includes its root certificate.

## Run

```bash
python -m searchiqs_scraper --check-access      # VPN + clearance check: 3 requests per address family
python -m searchiqs_scraper                     # scrape and publish to the Google Sheet
python -m searchiqs_scraper --local-only        # scrape only; files in output/<run id>/export/
python -m searchiqs_scraper --resume RUN_ID     # continue an interrupted run (same date range)
python -m searchiqs_scraper --export-only RUN_ID    # (re)publish a finished local run
python -m searchiqs_scraper --help
```

Local output per run: `output/<run id>/export/records.csv` (UTF-8 with BOM), `sheet_rows.json`, `report.json`.

| Exit code | Meaning |
|---|---|
| 0 | Scrape COMPLETE (and published, unless `--local-only`) |
| 2 | Scrape INCOMPLETE (data kept, gaps listed), or publishing failed after a good scrape |
| 1 | FAILED: no access, configuration error, fatal error |

An INCOMPLETE run is not published unless you pass `--publish-incomplete`. If the Cloudflare cookie expires
mid-run, the run stops cleanly: refresh the cookie and use `--resume`.

## Output columns

`Party 1`, `Party 2`, `Type`, `Book-Page`, `Date`, `Description`, `Additional Description`, `Related`, `Doc ID`, `Issues`

- Multiple parties are joined with `; ` in the site's order. Party lists are cross-checked with the site's full
  Grantor/Grantee tooltip.
- `Related` keeps the link labels (e.g. `BK: 12 PG:345`), joined with `; `.
- Values are written as plain text (`007` stays `007`). Empty cells mean the site showed nothing.
- `Issues` lists anything the scraper couldn't verify for that row. Empty means clean.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

Tests run fully offline. They use a synthetic fake of the site, built from its observed page structure, and a fake
Sheets backend. Covered: guest flow, pagination, parsing, two-pass verification, cap splitting, crash-safe resume,
and publishing.

## Status and limitations

- **Live verification:** on 2026-09-26/27 (US time), through a US VPN, the site flow in this package fetched the full
  range 07/08/2026–09/26/2026: **159 Land Records over 2 pages**, matching the site's own total, with no row issues.
  Two independent passes over a 1-day window agreed exactly, and an empty day was recognised as zero results.
- **Sample sheet:** built with this scraper's parser from the result pages captured during that live run. Later the
  same day, Cloudflare stopped accepting freshly issued clearance cookies from the VPN addresses used. A final
  end-to-end run including the live publish could not be completed before the deadline.
- **Manual step:** the scraper needs a browser-issued Cloudflare cookie per session, and plain HTTP can't obtain one.
  How long a cookie stays valid is set by the site (hours in testing), and the site can refuse cookies from some VPN IPs.
- **What the guarantee means:** COMPLETE means two independent observations agreed with each other and with the
  site's count. It doesn't prove the source didn't change between or after them.
