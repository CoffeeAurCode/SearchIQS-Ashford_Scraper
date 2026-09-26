from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping

FIELDS: tuple[str, ...] = (
    "Party 1",
    "Party 2",
    "Type",
    "Book-Page",
    "Date",
    "Description",
    "Additional Description",
    "Related",
)
EXTRA_COLUMNS: tuple[str, ...] = ("Doc ID", "Issues")
OUTPUT_COLUMNS: tuple[str, ...] = FIELDS + EXTRA_COLUMNS

PARSER_VERSION = 1


class FieldState(StrEnum):
    VALUE = "value"
    EMPTY = "empty"
    FAILED = "failed"


class WindowStatus(StrEnum):
    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"
    SPLIT = "SPLIT"


class Outcome(StrEnum):
    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"
    FAILED = "FAILED"

    @property
    def exit_code(self) -> int:
        return {"COMPLETE": 0, "INCOMPLETE": 2, "FAILED": 1}[self.value]


class ExportStatus(StrEnum):
    NOT_ATTEMPTED = "not_attempted"
    PUBLISHED = "published"
    FAILED = "failed"
    UNKNOWN = "unknown"


class CountUnit(StrEnum):
    UNVERIFIED = "unverified"
    ROWS = "rows"
    DOCUMENTS = "documents"


@dataclass(frozen=True)
class FieldValue:
    state: FieldState
    text: str = ""
    reason: str = ""

    def __post_init__(self) -> None:
        state = FieldState(self.state)
        object.__setattr__(self, "state", state)
        if state is FieldState.VALUE and (not self.text or self.reason):
            raise ValueError("VALUE needs non-empty text and no reason")
        if state is FieldState.EMPTY and (self.text or self.reason):
            raise ValueError("EMPTY carries no text or reason")
        if state is FieldState.FAILED and (self.text or not self.reason):
            raise ValueError("FAILED needs a reason and no text")

    @classmethod
    def of(cls, text: str) -> FieldValue:
        return cls(FieldState.VALUE, text) if text else cls(FieldState.EMPTY)

    @classmethod
    def empty(cls) -> FieldValue:
        return cls(FieldState.EMPTY)

    @classmethod
    def failed(cls, reason: str) -> FieldValue:
        return cls(FieldState.FAILED, reason=reason)

    @property
    def display(self) -> str:
        return self.text

    def to_json(self) -> dict[str, str]:
        out = {"state": self.state.value}
        if self.text:
            out["text"] = self.text
        if self.reason:
            out["reason"] = self.reason
        return out

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> FieldValue:
        if set(data) - {"state", "text", "reason"}:
            raise ValueError(f"unexpected field keys: {sorted(data)}")
        return cls(FieldState(data["state"]), _str(data.get("text", "")), _str(data.get("reason", "")))


@dataclass(frozen=True)
class Record:
    fields: Mapping[str, FieldValue]
    doc_id: str | None = None
    date_iso: str | None = None
    issues: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if tuple(self.fields) != FIELDS:
            raise ValueError(f"record fields must be exactly {FIELDS} in order")
        object.__setattr__(self, "issues", tuple(self.issues))

    @property
    def failed_fields(self) -> list[str]:
        return [name for name, v in self.fields.items() if v.state is FieldState.FAILED]

    @property
    def has_problems(self) -> bool:
        return bool(self.issues) or bool(self.failed_fields)

    def issue_texts(self) -> list[str]:
        failed = [f"{name}: FAILED({self.fields[name].reason})" for name in self.failed_fields]
        return failed + list(self.issues)

    def to_row(self) -> list[str]:
        return [self.fields[name].display for name in FIELDS] + [
            self.doc_id or "",
            "; ".join(self.issue_texts()),
        ]

    def to_json(self) -> dict[str, Any]:
        return {
            "fields": {name: self.fields[name].to_json() for name in FIELDS},
            "doc_id": self.doc_id,
            "date_iso": self.date_iso,
            "issues": list(self.issues),
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Record:
        raw_fields = data["fields"]
        if set(raw_fields) != set(FIELDS):
            raise ValueError("record JSON has wrong field names")
        return cls(
            fields={name: FieldValue.from_json(raw_fields[name]) for name in FIELDS},
            doc_id=_opt_str(data.get("doc_id")),
            date_iso=_opt_str(data.get("date_iso")),
            issues=tuple(_str(i) for i in data.get("issues", [])),
        )

    def sort_key(self) -> tuple[str, str]:
        return (self.date_iso or "", canonical_json(self.to_json()))


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _str(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError(f"expected string, got {type(value).__name__}")
    return value


def _opt_str(value: Any) -> str | None:
    return None if value is None else _str(value)
