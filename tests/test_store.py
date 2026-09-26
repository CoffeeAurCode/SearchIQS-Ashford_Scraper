import csv
import errno
import io
import json
import os
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

from searchiqs_scraper import store as store_mod
from searchiqs_scraper.dates import SITE_TZ, DateRange
from searchiqs_scraper.models import FIELDS, OUTPUT_COLUMNS, CountUnit, FieldValue, Outcome, Record, WindowStatus
from searchiqs_scraper.store import (
    DATE_SPLIT,
    InventoryCorrupt,
    RunLocked,
    RunMeta,
    RunStore,
    StoreError,
    WindowNode,
    atomic_write_bytes,
    build_export,
    json_bytes,
    new_run_id,
    parse_window_key,
    seal,
    window_key,
)

REPO = Path(__file__).resolve().parents[1]
RUN_NOW = datetime(2026, 9, 26, 12, 0, tzinfo=SITE_TZ)


def make_meta(run_id="run1"):
    return RunMeta(run_id, RUN_NOW, "US", CountUnit.ROWS, {"max_pages": 1000})


def record(day, text="x", **field_overrides):
    fields = {name: FieldValue.of(f"{name} {text}") for name in FIELDS}
    fields["Date"] = FieldValue.of(f"{day:%m/%d/%Y}")
    fields.update(field_overrides)
    return Record(fields=fields, doc_id=f"D-{day}-{text}", date_iso=day.isoformat())


def leaf_node(rng, parent, status=WindowStatus.COMPLETE, records=None, attempt=1, reason=None):
    records = records if records is not None else [record(rng.start, "a"), record(rng.end, "b")]
    rows = [{"fingerprint": r.doc_id} for r in records]
    if status is WindowStatus.INCOMPLETE and reason is None:
        reason = "cap"
    return WindowNode(rng, status, parent, attempt=attempt, total=len(rows), counts={"rows": len(rows)},
                      reason=reason, rows=rows, records=records)


def split_node(rng, parent, children=None):
    children = children if children is not None else [window_key(c) for c in rng.split_half()]
    return WindowNode(rng, WindowStatus.SPLIT, parent, children=children, split_kind=DATE_SPLIT, reason="cap")


def write_raw(store, meta, node, tamper=None):
    data = node.to_json(meta.run_id)
    if tamper:
        tamper(data)
    store.windows_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(store.windows_dir / f"{node.key}.json", json_bytes(seal(data)))


@pytest.fixture
def ctx(tmp_path):
    meta = make_meta()
    store = RunStore(tmp_path, meta.run_id)
    with store.locked(create=True):
        store.create(meta)
        yield store, meta


def root_key(meta):
    return window_key(meta.range)


def test_window_keys_are_strict():
    rng = DateRange(date(2026, 7, 8), date(2026, 9, 26))
    assert parse_window_key(window_key(rng)) == rng
    for bad in ("2026-7-08_2026-09-26", "../2026-07-08_2026-09-26", "2026-02-30_2026-03-01", "2026-09-26_2026-07-08"):
        with pytest.raises(ValueError):
            parse_window_key(bad)


def test_run_ids_are_path_safe(tmp_path):
    assert store_mod.RUN_ID_RE.fullmatch(new_run_id(RUN_NOW))
    for bad in ("../x", "a/b", "", "a" * 65, "a b"):
        with pytest.raises(StoreError):
            RunStore(tmp_path, bad)


def test_run_json_round_trip_and_single_creation(ctx):
    store, meta = ctx
    loaded = store.load_run()
    assert loaded == meta
    assert loaded.range.days == 81 and loaded.range.end == date(2026, 9, 26)
    with pytest.raises(StoreError, match="already exists"):
        store.create(meta)


def test_run_json_missing_or_corrupt_fails(ctx):
    store, meta = ctx
    raw = json.loads(store.run_json.read_text(encoding="utf-8"))
    raw["date_from"] = "2026-01-01"
    store.run_json.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(StoreError, match="corrupt"):
        store.load_run()
    store.run_json.write_bytes(json_bytes(seal({k: v for k, v in raw.items() if k != "sha256"})))
    with pytest.raises(StoreError, match="date range"):
        store.load_run()
    store.run_json.write_text("{", encoding="utf-8")
    with pytest.raises(StoreError, match="corrupt"):
        store.load_run()
    store.run_json.unlink()
    with pytest.raises(StoreError, match="missing"):
        store.load_run()


def test_mutation_requires_lock(tmp_path):
    meta = make_meta()
    store = RunStore(tmp_path, meta.run_id)
    with pytest.raises(StoreError, match="locked"):
        store.create(meta)
    with pytest.raises(StoreError, match="no run directory"):
        with store.locked():
            pass


LOCK_PROBE = """
import sys
from pathlib import Path
from searchiqs_scraper.store import FileLock, RunLocked
lock = FileLock(Path(sys.argv[1]))
try:
    lock.acquire()
except RunLocked:
    sys.exit(3)
lock.release()
"""


def probe_lock(path):
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    return subprocess.run([sys.executable, "-c", LOCK_PROBE, str(path)], env=env, timeout=60).returncode


def test_run_lock_excludes_other_processes(ctx):
    store, _ = ctx
    lock_path = store.dir / ".lock"
    assert probe_lock(lock_path) == 3
    with pytest.raises(RunLocked):
        with RunStore(store.dir.parent, store.run_id).locked():
            pass


def test_run_lock_released_after_use(tmp_path):
    meta = make_meta()
    store = RunStore(tmp_path, meta.run_id)
    with store.locked(create=True):
        store.create(meta)
    assert probe_lock(store.dir / ".lock") == 0


def test_commit_and_resolve_single_root(ctx):
    store, meta = ctx
    store.commit(meta, leaf_node(meta.range, None))
    res = store.resolve(meta)
    assert res.complete and [leaf.key for leaf in res.leaves] == [root_key(meta)]
    assert res.orphans == ()


def test_empty_store_root_is_missing_gap(ctx):
    store, meta = ctx
    res = store.resolve(meta)
    assert [(leaf.key, leaf.problem) for leaf in res.gaps] == [(root_key(meta), "missing")]


def test_commit_validation(ctx):
    store, meta = ctx
    outside = DateRange(date(2026, 1, 1), date(2026, 1, 2))
    with pytest.raises(StoreError, match="outside"):
        store.commit(meta, leaf_node(outside, root_key(meta)))
    left, _ = meta.range.split_half()
    with pytest.raises(StoreError, match="root"):
        store.commit(meta, leaf_node(left, None))
    with pytest.raises(StoreError, match="root"):
        store.commit(meta, leaf_node(meta.range, "someone"))
    with pytest.raises(ValueError):
        store.commit(meta, split_node(meta.range, None, children=[window_key(left)]))
    with pytest.raises(ValueError):
        WindowNode(meta.range, WindowStatus.INCOMPLETE, None)
    with pytest.raises(ValueError):
        WindowNode(meta.range, WindowStatus.SPLIT, None, children=["x"], split_kind=DATE_SPLIT, rows=[{}])


def test_attempt_replaces_previous_without_duplicates(ctx):
    store, meta = ctx
    store.commit(meta, leaf_node(meta.range, None, WindowStatus.INCOMPLETE, [record(meta.range.start, "old")]))
    store.commit(meta, leaf_node(meta.range, None, records=[record(meta.range.start, "new")], attempt=2))
    assert len(list(store.windows_dir.glob("*.json"))) == 1
    export = build_export(meta, store.resolve(meta))
    assert [r.doc_id for r in export.records] == [f"D-{meta.range.start}-new"]
    assert export.outcome is Outcome.COMPLETE


@pytest.mark.parametrize("failing", ["fsync", "replace", "write"])
def test_crash_during_commit_keeps_previous_version(ctx, monkeypatch, failing):
    store, meta = ctx
    store.commit(meta, leaf_node(meta.range, None, records=[record(meta.range.start, "v1")]))
    before = (store.windows_dir / f"{root_key(meta)}.json").read_bytes()

    def boom(*args, **kwargs):
        raise OSError(errno.ENOSPC, "simulated failure")

    class FullDiskFile:
        def __init__(self, fh):
            self.fh = fh

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.fh.close()

        write = staticmethod(boom)

    with monkeypatch.context() as m:
        if failing == "write":
            m.setattr(store_mod, "open", lambda path, mode: FullDiskFile(open(path, mode)), raising=False)
        else:
            m.setattr(store_mod.os, failing, boom)
        with pytest.raises(OSError):
            store.commit(meta, leaf_node(meta.range, None, records=[record(meta.range.start, "v2")], attempt=2))
    assert (store.windows_dir / f"{root_key(meta)}.json").read_bytes() == before
    assert list(store.windows_dir.glob("*.tmp")) == []
    assert [r.doc_id for r in build_export(meta, store.resolve(meta)).records] == [f"D-{meta.range.start}-v1"]


def test_stale_tmp_files_ignored_and_removed(ctx):
    store, meta = ctx
    store.commit(meta, leaf_node(meta.range, None))
    stale = store.windows_dir / f"{root_key(meta)}.json.tmp"
    stale.write_text("{partial", encoding="utf-8")
    assert store.resolve(meta).complete
    assert store.cleanup_tmp() == [stale]
    assert not stale.exists()


@pytest.mark.parametrize(
    "damage",
    ["checksum", "partial", "foreign_run", "wrong_parent", "parser_version", "key_mismatch"],
)
def test_invalid_node_files_are_gaps(ctx, damage):
    store, meta = ctx
    left, right = meta.range.split_half()
    store.commit(meta, split_node(meta.range, None))
    store.commit(meta, leaf_node(right, root_key(meta)))
    path = store.windows_dir / f"{window_key(left)}.json"
    node = leaf_node(left, root_key(meta))
    if damage == "checksum":
        store.commit(meta, node)
        path.write_bytes(path.read_bytes().replace(b'"attempt": 1', b'"attempt": 9'))
    elif damage == "partial":
        store.commit(meta, node)
        path.write_bytes(path.read_bytes()[:40])
    elif damage == "foreign_run":
        write_raw(store, meta, node, lambda d: d.update(run_id="other"))
    elif damage == "wrong_parent":
        write_raw(store, meta, node, lambda d: d.update(parent=window_key(right)))
    elif damage == "parser_version":
        write_raw(store, meta, node, lambda d: d.update(parser_version=99))
    elif damage == "key_mismatch":
        path.write_bytes(json_bytes(seal(leaf_node(right, root_key(meta)).to_json(meta.run_id))))
    res = store.resolve(meta)
    assert [leaf.key for leaf in res.gaps] == [window_key(left)]
    assert res.gaps[0].problem.startswith("invalid")
    export = build_export(meta, res)
    assert export.outcome is Outcome.INCOMPLETE
    assert all(r.date_iso >= right.start.isoformat() for r in export.records)


def test_split_with_missing_sibling(ctx):
    store, meta = ctx
    left, right = meta.range.split_half()
    store.commit(meta, split_node(meta.range, None))
    store.commit(meta, leaf_node(left, root_key(meta)))
    res = store.resolve(meta)
    assert [(leaf.range, leaf.problem) for leaf in res.gaps] == [(right, "missing")]
    export = build_export(meta, res)
    assert export.outcome is Outcome.INCOMPLETE
    assert export.report["uncovered"] == [f"{right} (missing)"]
    assert len(export.records) == 2


def test_crash_after_split_commit_leaves_both_children_as_gaps(ctx):
    store, meta = ctx
    store.commit(meta, split_node(meta.range, None))
    res = store.resolve(meta)
    assert [leaf.range for leaf in res.gaps] == list(meta.range.split_half())


def test_nested_splits_resolve_in_date_order(ctx):
    store, meta = ctx
    left, right = meta.range.split_half()
    ll, lr = left.split_half()
    store.commit(meta, split_node(meta.range, None))
    store.commit(meta, split_node(left, root_key(meta)))
    for rng in (ll, lr):
        store.commit(meta, leaf_node(rng, window_key(left)))
    store.commit(meta, leaf_node(right, root_key(meta)))
    res = store.resolve(meta)
    assert res.complete and [leaf.range for leaf in res.leaves] == [ll, lr, right]


def test_stale_incomplete_parent_orphans_children(ctx):
    store, meta = ctx
    left, right = meta.range.split_half()
    store.commit(meta, split_node(meta.range, None))
    store.commit(meta, leaf_node(left, root_key(meta), records=[record(left.start, "child")]))
    store.commit(meta, leaf_node(right, root_key(meta), records=[record(right.start, "child")]))
    store.commit(meta, leaf_node(meta.range, None, WindowStatus.INCOMPLETE, [record(meta.range.start, "parent")]))
    res = store.resolve(meta)
    assert [(leaf.key, leaf.problem) for leaf in res.gaps] == [(root_key(meta), "incomplete: cap")]
    assert res.orphans == tuple(sorted(f"{window_key(r)}.json" for r in (left, right)))
    export = build_export(meta, res)
    assert [r.doc_id for r in export.records] == [f"D-{meta.range.start}-parent"]
    assert "window" in export.records[0].issues[-1]
    moved = store.move_orphans(res)
    assert sorted(p.name for p in moved) == list(res.orphans)
    assert all(p.parent.name == "orphaned" for p in moved)
    assert store.resolve(meta).orphans == ()


def test_corrupt_split_node_treated_as_missing(ctx):
    store, meta = ctx
    left, right = meta.range.split_half()
    store.commit(meta, split_node(meta.range, None))
    store.commit(meta, leaf_node(left, root_key(meta)))
    store.commit(meta, leaf_node(right, root_key(meta)))
    root_path = store.windows_dir / f"{root_key(meta)}.json"
    root_path.write_bytes(root_path.read_bytes()[:-20])
    res = store.resolve(meta)
    assert [leaf.key for leaf in res.gaps] == [root_key(meta)]
    assert len(res.orphans) == 2
    assert build_export(meta, res).records == ()


@pytest.mark.parametrize(
    "children",
    [
        lambda r: [window_key(DateRange(r.start, r.split_half()[0].end)), window_key(r.split_half()[0])],
        lambda r: [window_key(r.split_half()[0]), window_key(DateRange(r.split_half()[1].start, r.end.replace(day=1)))],
        lambda r: [window_key(DateRange(r.start, r.split_half()[0].end)),
                   window_key(DateRange(r.split_half()[0].end, r.end))],
        lambda r: [window_key(r)],
        lambda r: ["not-a-key", window_key(r.split_half()[1])],
    ],
    ids=["repeated", "gap", "overlap", "self", "bad-key"],
)
def test_bad_inventory_is_corrupt(ctx, children):
    store, meta = ctx
    bad = WindowNode(meta.range, WindowStatus.SPLIT, None, children=children(meta.range), split_kind=DATE_SPLIT)
    write_raw(store, meta, bad)
    with pytest.raises(InventoryCorrupt):
        store.resolve(meta)


def test_unsupported_split_kind_is_corrupt(ctx):
    store, meta = ctx
    write_raw(store, meta, split_node(meta.range, None), lambda d: d.update(split_kind="doc_type"))
    with pytest.raises(InventoryCorrupt):
        store.resolve(meta)


def test_failed_field_makes_export_incomplete(ctx):
    store, meta = ctx
    bad = record(meta.range.start, "f", **{"Related": FieldValue.failed("detail unavailable")})
    store.commit(meta, leaf_node(meta.range, None, records=[bad]))
    export = build_export(meta, store.resolve(meta))
    assert export.outcome is Outcome.INCOMPLETE
    assert export.report["records_with_issues"] == 1
    assert export.tables.records[1][-1] == "Related: FAILED(detail unavailable)"


def test_zero_records_complete_export(ctx):
    store, meta = ctx
    store.commit(meta, leaf_node(meta.range, None, records=[]))
    export = build_export(meta, store.resolve(meta))
    assert export.outcome is Outcome.COMPLETE
    assert export.tables.records == (OUTPUT_COLUMNS,)
    assert export.csv_bytes() == ("﻿" + ",".join(OUTPUT_COLUMNS) + "\r\n").encode("utf-8")


def test_export_is_deterministic_across_window_layouts(tmp_path):
    meta = make_meta()
    left, right = meta.range.split_half()
    recs_left = [record(left.end, "z"), record(left.start, "b"), record(left.start, "a")]
    recs_right = [record(right.end, "q"), record(right.start, "m")]

    split_store = RunStore(tmp_path / "split", meta.run_id)
    with split_store.locked(create=True):
        split_store.create(meta)
        split_store.commit(meta, split_node(meta.range, None))
        split_store.commit(meta, leaf_node(right, root_key(meta), records=recs_right))
        split_store.commit(meta, leaf_node(left, root_key(meta), records=recs_left))
        split_export = build_export(meta, split_store.resolve(meta))
        split_store.write_export(split_export)
        first = {p.name: p.read_bytes() for p in split_store.export_dir.iterdir()}
        split_store.write_export(build_export(meta, split_store.resolve(meta)))
        second = {p.name: p.read_bytes() for p in split_store.export_dir.iterdir()}

    flat_store = RunStore(tmp_path / "flat", meta.run_id)
    with flat_store.locked(create=True):
        flat_store.create(meta)
        flat_store.commit(meta, leaf_node(meta.range, None, records=list(reversed(recs_left + recs_right))))
        flat_export = build_export(meta, flat_store.resolve(meta))

    assert first == second and set(first) == {"records.csv", "sheet_rows.json", "report.json"}
    assert split_export.csv_bytes() == flat_export.csv_bytes()
    assert split_export.tables == flat_export.tables
    dates = [r.date_iso for r in split_export.records]
    assert dates == sorted(dates)


def test_csv_format(ctx):
    store, meta = ctx
    tricky = record(meta.range.start, "t", **{
        "Description": FieldValue.of('line1\nline2 "quoted", comma'),
        "Book-Page": FieldValue.of("0012-0034"),
        "Party 1": FieldValue.of("=1+1"),
    })
    store.commit(meta, leaf_node(meta.range, None, records=[tricky]))
    data = build_export(meta, store.resolve(meta)).csv_bytes()
    assert data.startswith("﻿".encode("utf-8"))
    text = data.decode("utf-8-sig")
    assert text.endswith("\r\n")
    parsed = list(csv.reader(io.StringIO(text, newline="")))
    assert parsed == [list(OUTPUT_COLUMNS), tricky.to_row()]
