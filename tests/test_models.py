import pytest

from searchiqs_scraper.models import FIELDS, OUTPUT_COLUMNS, FieldState, FieldValue, Outcome, Record


def make_record(**overrides):
    fields = {name: FieldValue.of(f"{name} value") for name in FIELDS}
    fields.update(overrides)
    return Record(fields=fields, doc_id="D1", date_iso="2026-09-01")


def test_output_columns_order():
    assert OUTPUT_COLUMNS == (
        "Party 1", "Party 2", "Type", "Book-Page", "Date", "Description",
        "Additional Description", "Related", "Doc ID", "Issues",
    )


def test_field_value_states():
    assert FieldValue.of("").state is FieldState.EMPTY
    assert FieldValue.of("x").state is FieldState.VALUE
    with pytest.raises(ValueError):
        FieldValue(FieldState.VALUE, "")
    with pytest.raises(ValueError):
        FieldValue(FieldState.EMPTY, "x")
    with pytest.raises(ValueError):
        FieldValue(FieldState.FAILED)
    with pytest.raises(ValueError):
        FieldValue(FieldState.FAILED, "x", "why")


@pytest.mark.parametrize("value", [FieldValue.of(" 007\nline "), FieldValue.empty(), FieldValue.failed("truncated")])
def test_field_value_json_round_trip(value):
    assert FieldValue.from_json(value.to_json()) == value


def test_record_requires_exact_field_order():
    fields = {name: FieldValue.empty() for name in reversed(FIELDS)}
    with pytest.raises(ValueError):
        Record(fields=fields)


def test_record_row_blank_vs_failed():
    record = make_record(**{"Description": FieldValue.failed("detail fetch error"), "Related": FieldValue.empty()})
    row = record.to_row()
    assert row[FIELDS.index("Description")] == ""
    assert row[FIELDS.index("Related")] == ""
    assert row[-1] == "Description: FAILED(detail fetch error)"
    assert record.has_problems


def test_record_json_round_trip():
    record = make_record(**{"Book-Page": FieldValue.of("0012-0034")})
    record = Record(record.fields, None, None, ("note",))
    assert Record.from_json(record.to_json()) == record


def test_outcome_exit_codes():
    assert [o.exit_code for o in Outcome] == [0, 2, 1]
