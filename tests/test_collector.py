from dataclasses import replace
from datetime import date, datetime

import pytest

from searchiqs_scraper.collector import Collector, assess, passes_agree
from searchiqs_scraper.config import Config
from searchiqs_scraper.dates import SITE_TZ, DateRange
from searchiqs_scraper.flow import PassResult, SiteFlow
from searchiqs_scraper.http import DeadlineExceeded, HttpClient, TransportError
from searchiqs_scraper.models import FIELDS, CountUnit, FieldValue, Outcome, Record, WindowStatus
from searchiqs_scraper.parser import GridRow, fingerprint
from searchiqs_scraper.store import RunMeta, RunStore, build_export, window_key

from fakesite import FakeSite, make_docs

RUN_NOW = datetime(2026, 9, 26, 12, 0, tzinfo=SITE_TZ)
FULL = DateRange(date(2026, 7, 8), date(2026, 9, 26))
ROOT = window_key(FULL)


def make_meta(unit=CountUnit.DOCUMENTS):
    return RunMeta("run1", RUN_NOW, "US", unit, {})


class Runner:
    """One fresh fake-site session per pass; `site_for(n)` builds the site for pass n (or an exception to raise)."""

    def __init__(self, site_for, max_pages=1000):
        self.site_for = site_for
        self.max_pages = max_pages
        self.calls = []

    def __call__(self, window):
        self.calls.append(window)
        site = self.site_for(len(self.calls))
        if isinstance(site, BaseException):
            raise site
        client = HttpClient(site, Config(), sleep=lambda s: None, rng=lambda: 0.0)
        return SiteFlow(client, max_pages=self.max_pages).run_pass(window)


def collect(root, runner, meta=None, egress=lambda: "US", max_passes=6):
    meta = meta or make_meta()
    store = RunStore(root, meta.run_id)
    with store.locked(create=True):
        if not store.run_json.exists():
            store.create(meta)
        result = Collector(store, meta, runner, check_egress=egress, max_passes=max_passes).run()
        resolution = store.resolve(meta)
        export = build_export(meta, resolution, result.stopped)
        store.write_export(export)
    return store, result, resolution, export


def node(store, key, meta=None):
    found, problem = store.read_node(meta or make_meta(), key, _parent_of(store, key, meta or make_meta()))
    assert found is not None, problem
    return found


def _parent_of(store, key, meta):
    for leaf_parent in _walk(store, meta):
        if leaf_parent[0] == key:
            return leaf_parent[1]
    raise KeyError(key)


def _walk(store, meta):
    stack = [(ROOT, None)]
    while stack:
        key, parent = stack.pop()
        yield key, parent
        found, _ = store.read_node(meta, key, parent)
        if found is not None and found.status is WindowStatus.SPLIT:
            stack.extend((c, key) for c in found.children)


def export_bytes(store):
    return {p.name: p.read_bytes() for p in sorted(store.export_dir.iterdir())}


def edited(docs, index, text):
    docs = list(docs)
    docs[index] = replace(docs[index], description=text)
    return docs


DOCS = make_docs(159)


def test_single_window_two_agreeing_passes_complete(tmp_path):
    runner = Runner(lambda n: FakeSite(DOCS))
    store, result, resolution, export = collect(tmp_path, runner)
    root = node(store, ROOT)
    assert root.status is WindowStatus.COMPLETE and root.total == 159
    assert root.counts == {"rows": 159, "doc_ids": 159, "passes": 2, "pass_errors": 0}
    assert len(runner.calls) == 2 and result.windows_committed == 1
    assert export.outcome is Outcome.COMPLETE
    assert sorted(r.doc_id for r in export.records) == sorted(d.record_id for d in DOCS)
    assert {r["doc_id"] for r in root.rows} == {d.record_id for d in DOCS}


def test_explicit_zero_results_complete_with_headers_only(tmp_path):
    runner = Runner(lambda n: FakeSite(make_docs(3, start=date(2025, 1, 1))))
    store, _, _, export = collect(tmp_path, runner)
    assert node(store, ROOT).status is WindowStatus.COMPLETE
    assert export.outcome is Outcome.COMPLETE and export.records == ()
    assert (store.export_dir / "records.csv").read_bytes().count(b"\r\n") == 1


def test_edited_row_between_passes_is_retried_until_two_agree(tmp_path):
    runner = Runner(lambda n: FakeSite(edited(DOCS, 5, "CHANGED") if n == 1 else DOCS))
    store, _, _, export = collect(tmp_path, runner)
    assert len(runner.calls) == 3
    assert node(store, ROOT).status is WindowStatus.COMPLETE
    assert export.outcome is Outcome.COMPLETE


def test_swapped_row_same_count_never_agreeing_is_unstable(tmp_path):
    swapped = DOCS[:-1] + [replace(DOCS[-1], record_id="L|999999", book_page="99-999")]
    runner = Runner(lambda n: FakeSite(DOCS if n % 2 else swapped))
    store, _, _, export = collect(tmp_path, runner, max_passes=4)
    root = node(store, ROOT)
    assert len(runner.calls) == 4
    assert root.status is WindowStatus.INCOMPLETE and root.reason.startswith("unstable")
    assert len(root.records) == 159
    assert export.outcome is Outcome.INCOMPLETE
    assert all(any("unstable" in i for i in r.issues) for r in export.records)


def test_capped_window_splits_by_date_until_uncapped(tmp_path):
    runner = Runner(lambda n: FakeSite(DOCS, cap=100))
    store, _, resolution, export = collect(tmp_path, runner)
    root = node(store, ROOT)
    assert root.status is WindowStatus.SPLIT and root.reason == "count shortfall: collected 100 of 159"
    assert [leaf.node.status for leaf in resolution.leaves] == [WindowStatus.COMPLETE] * 2
    assert export.outcome is Outcome.COMPLETE and len(export.records) == 159


def test_single_day_cap_ends_incomplete_without_endless_recursion(tmp_path):
    day = date(2026, 8, 3)
    docs = [replace(d, recorded=day) for d in make_docs(5)]
    runner = Runner(lambda n: FakeSite(docs, cap=3))
    store, _, resolution, export = collect(tmp_path, runner)
    gaps = resolution.gaps
    assert [g.range for g in gaps] == [DateRange(day, day)]
    assert gaps[0].node.reason == "count shortfall: collected 3 of 5"
    assert export.outcome is Outcome.INCOMPLETE and len(export.records) == 3
    assert len(runner.calls) <= 2 * 2 * 8


def test_page_limit_splits_instead_of_certifying(tmp_path):
    runner = Runner(lambda n: FakeSite(DOCS), max_pages=1)
    store, _, resolution, export = collect(tmp_path, runner)
    root = node(store, ROOT)
    assert root.status is WindowStatus.SPLIT and root.reason.startswith("page-limit")
    assert len(runner.calls) == 1 + 2 * 2
    assert export.outcome is Outcome.COMPLETE and len(export.records) == 159


def test_ambiguous_post_redone_with_fresh_session(tmp_path):
    runner = Runner(lambda n: FakeSite(DOCS, fail_on={5: TransportError("reset")}) if n == 1 else FakeSite(DOCS))
    store, _, _, export = collect(tmp_path, runner)
    root = node(store, ROOT)
    assert len(runner.calls) == 3
    assert root.status is WindowStatus.COMPLETE and root.issues == ["ambiguous-post"]
    assert export.outcome is Outcome.COMPLETE


def test_persistent_pass_errors_end_incomplete(tmp_path):
    runner = Runner(lambda n: FakeSite(DOCS, ignore_group=True))
    store, _, _, export = collect(tmp_path, runner, max_passes=3)
    root = node(store, ROOT)
    assert len(runner.calls) == 3
    assert root.status is WindowStatus.INCOMPLETE
    assert root.reason == "pass errors: criteria-mismatch, criteria-mismatch, criteria-mismatch"
    assert root.records == [] and root.total is None
    assert export.outcome is Outcome.INCOMPLETE


def test_one_good_pass_is_kept_when_verification_keeps_failing(tmp_path):
    runner = Runner(lambda n: FakeSite(DOCS) if n == 1 else FakeSite(DOCS, ignore_group=True))
    store, _, _, export = collect(tmp_path, runner, max_passes=3)
    root = node(store, ROOT)
    assert root.status is WindowStatus.INCOMPLETE and len(root.records) == 159
    assert export.outcome is Outcome.INCOMPLETE and len(export.records) == 159


def test_challenge_stops_the_run_and_resume_finishes(tmp_path):
    first = Runner(lambda n: FakeSite(DOCS, challenge_on={3}) if n == 2 else FakeSite(DOCS))
    store, result, resolution, export = collect(tmp_path / "a", first)
    assert len(first.calls) == 2
    assert result.stop_code == "cloudflare-challenge" and "refresh SEARCHIQS_CF_CLEARANCE" in result.stopped
    assert not (store.windows_dir / f"{ROOT}.json").exists()
    assert export.outcome is Outcome.INCOMPLETE and export.report["stopped"] == result.stopped

    second = Runner(lambda n: FakeSite(DOCS))
    store, result, _, export = collect(tmp_path / "a", second)
    assert result.stopped is None and export.outcome is Outcome.COMPLETE

    fresh, *_ = collect(tmp_path / "b", Runner(lambda n: FakeSite(DOCS)))
    assert export_bytes(store) == export_bytes(fresh)


@pytest.mark.parametrize("interrupt_on", [3, 4, 5, 6])
def test_interrupted_run_resumes_only_gaps_and_matches_uninterrupted(tmp_path, interrupt_on):
    def site_for(n):
        return KeyboardInterrupt() if n == interrupt_on else FakeSite(DOCS, cap=100)

    with pytest.raises(KeyboardInterrupt):
        collect(tmp_path / "a", Runner(site_for))
    resumed = Runner(lambda n: FakeSite(DOCS, cap=100))
    store, _, _, export = collect(tmp_path / "a", resumed)
    expected_calls = {3: 4, 4: 4, 5: 2, 6: 2}[interrupt_on]
    assert len(resumed.calls) == expected_calls
    assert export.outcome is Outcome.COMPLETE

    fresh, *_ = collect(tmp_path / "b", Runner(lambda n: FakeSite(DOCS, cap=100)))
    assert export_bytes(store) == export_bytes(fresh)


def test_resume_redoes_incomplete_leaf_as_next_attempt(tmp_path):
    collect(tmp_path, Runner(lambda n: FakeSite(DOCS, ignore_group=True)), max_passes=2)
    store, _, _, export = collect(tmp_path, Runner(lambda n: FakeSite(DOCS)))
    root = node(store, ROOT)
    assert (root.status, root.attempt) == (WindowStatus.COMPLETE, 2)
    assert export.outcome is Outcome.COMPLETE


def test_deadline_stops_the_run(tmp_path):
    runner = Runner(lambda n: DeadlineExceeded("run deadline reached"))
    _, result, _, export = collect(tmp_path, runner)
    assert result.stop_code == "deadline-exceeded" and len(runner.calls) == 1
    assert export.outcome is Outcome.INCOMPLETE


def test_non_us_egress_stops_before_any_site_request(tmp_path):
    runner = Runner(lambda n: FakeSite(DOCS))
    _, result, _, _ = collect(tmp_path, runner, egress=lambda: "IN")
    assert result.stop_code == "egress-check-failed" and "expected US" in result.stopped
    assert runner.calls == []


def test_egress_rechecked_at_every_window(tmp_path):
    checks = []
    collect(tmp_path, Runner(lambda n: FakeSite(DOCS, cap=100)), egress=lambda: checks.append(1) or "US")
    assert len(checks) == 3


def test_unverified_count_unit_never_completes(tmp_path):
    meta = make_meta(CountUnit.UNVERIFIED)
    store, _, _, export = collect(tmp_path, Runner(lambda n: FakeSite(DOCS)), meta=meta)
    assert node(store, ROOT, meta).reason.startswith("unresolved-count-unit")
    assert export.outcome is Outcome.INCOMPLETE


def grid_row(doc_id, text="x"):
    fields = {name: FieldValue.of(f"{name} {text}") for name in FIELDS}
    record = Record(fields=fields, doc_id=doc_id, date_iso="2026-08-01")
    return GridRow(record, (), fingerprint(record, ()))


def pass_result(total, rows):
    return PassResult(FULL, total, tuple(rows), 1)


def test_passes_compared_as_multisets():
    a, b = grid_row("L|1"), grid_row("L|2")
    assert passes_agree(pass_result(3, [a, a, b]), pass_result(3, [b, a, a]))
    assert not passes_agree(pass_result(3, [a, a, b]), pass_result(3, [a, b, b]))
    assert not passes_agree(pass_result(3, [a, b]), pass_result(2, [a, b]))


@pytest.mark.parametrize("unit, total, rows, status, reason", [
    (CountUnit.DOCUMENTS, 2, [grid_row("L|1"), grid_row("L|2")], WindowStatus.COMPLETE, None),
    (CountUnit.DOCUMENTS, 2, [grid_row("L|1"), grid_row(None)], WindowStatus.INCOMPLETE,
     "unresolved-count-unit: rows without a document id"),
    (CountUnit.DOCUMENTS, 2, [grid_row("L|1"), grid_row("L|1", "y")], WindowStatus.INCOMPLETE,
     "duplicate document ids: 2 rows, 1 distinct"),
    (CountUnit.DOCUMENTS, 1, [grid_row("L|1"), grid_row("L|2")], WindowStatus.INCOMPLETE,
     "count excess: collected 2, site reports 1"),
    (CountUnit.ROWS, 2, [grid_row("L|1"), grid_row("L|1", "y")], WindowStatus.COMPLETE, None),
    (CountUnit.UNVERIFIED, 2, [grid_row("L|1"), grid_row("L|2")], WindowStatus.INCOMPLETE,
     "unresolved-count-unit: the site's counting unit is not verified"),
])
def test_assess_uses_only_the_verified_unit(unit, total, rows, status, reason):
    verdict = assess(pass_result(total, rows), unit)
    assert (verdict.status, verdict.reason, verdict.split) == (status, reason, False)


def test_shortfall_requests_a_split():
    verdict = assess(pass_result(3, [grid_row("L|1")]), CountUnit.DOCUMENTS)
    assert verdict.split and verdict.reason == "count shortfall: collected 1 of 3"
