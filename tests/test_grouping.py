from datetime import datetime

from searchiqs_scraper.dates import SITE_TZ
from searchiqs_scraper.grouping import group_records
from searchiqs_scraper.models import FIELDS, CountUnit, FieldValue, Outcome, Record, WindowStatus
from searchiqs_scraper.store import DATE_SPLIT, RunMeta, RunStore, WindowNode, build_export, window_key


def record(doc_id, text="x", issues=()):
    fields = {name: FieldValue.of(f"{name} {text}") for name in FIELDS}
    return Record(fields=fields, doc_id=doc_id, date_iso="2026-08-01", issues=issues)


def test_distinct_documents_pass_through():
    grouped = group_records([record("L|1"), record("L|2")])
    assert [r.doc_id for r in grouped.records] == ["L|1", "L|2"]
    assert (grouped.duplicates_removed, grouped.conflicts) == (0, ())


def test_identical_duplicate_kept_once_with_issues_merged():
    grouped = group_records([record("L|1"), record("L|1", issues=("late",))])
    assert len(grouped.records) == 1 and grouped.records[0].issues == ("late",)
    assert grouped.duplicates_removed == 1


def test_conflicting_duplicate_keeps_both_and_flags_them():
    grouped = group_records([record("L|1", "a"), record("L|1", "b"), record("L|2")])
    assert [r.doc_id for r in grouped.records] == ["L|1", "L|1", "L|2"]
    assert grouped.conflicts == ("L|1",)
    assert all("identity conflict" in r.issues[-1] for r in grouped.records[:2])
    assert not grouped.records[2].issues


def test_records_without_doc_id_are_never_grouped():
    grouped = group_records([record(None), record(None)])
    assert len(grouped.records) == 2 and grouped.duplicates_removed == 0


def test_export_dedupes_across_windows_and_flags_conflicts(tmp_path):
    meta = RunMeta("run1", datetime(2026, 9, 26, 12, 0, tzinfo=SITE_TZ), "US", CountUnit.DOCUMENTS, {})
    left, right = meta.range.split_half()
    store = RunStore(tmp_path, "run1")

    def build(right_records):
        with store.locked(create=True):
            if not store.run_json.exists():
                store.create(meta)
            store.commit(meta, WindowNode(meta.range, WindowStatus.SPLIT, None, reason="cap", split_kind=DATE_SPLIT,
                                          children=[window_key(left), window_key(right)]))
            for rng, recs in ((left, [record("L|1")]), (right, right_records)):
                store.commit(meta, WindowNode(rng, WindowStatus.COMPLETE, window_key(meta.range), total=len(recs),
                                              records=recs))
            return build_export(meta, store.resolve(meta))

    same = build([record("L|1"), record("L|2")])
    assert same.outcome is Outcome.COMPLETE and len(same.records) == 2
    assert same.report["duplicates_removed"] == 1

    clash = build([record("L|1", "changed")])
    assert clash.outcome is Outcome.INCOMPLETE and len(clash.records) == 2
    assert clash.report["identity_conflicts"] == ["L|1"]
