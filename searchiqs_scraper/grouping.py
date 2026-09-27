from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable

from .models import Record, canonical_json


@dataclass(frozen=True)
class Grouped:
    records: tuple[Record, ...]
    duplicates_removed: int
    conflicts: tuple[str, ...]


def _content(record: Record) -> str:
    data = record.to_json()
    del data["issues"]
    return canonical_json(data)


def group_records(records: Iterable[Record]) -> Grouped:
    out: list[Record] = []
    by_id: dict[str, list[int]] = {}
    removed = 0
    for record in records:
        if record.doc_id is None:
            out.append(record)
            continue
        indices = by_id.setdefault(record.doc_id, [])
        match = next((i for i in indices if _content(out[i]) == _content(record)), None)
        if match is None:
            indices.append(len(out))
            out.append(record)
            continue
        kept = out[match]
        out[match] = replace(kept, issues=kept.issues + tuple(i for i in record.issues if i not in kept.issues))
        removed += 1
    conflicts = tuple(sorted(doc_id for doc_id, indices in by_id.items() if len(indices) > 1))
    for doc_id in conflicts:
        note = f"identity conflict: document {doc_id} appears with different content"
        for i in by_id[doc_id]:
            out[i] = replace(out[i], issues=out[i].issues + (note,))
    return Grouped(tuple(out), removed, conflicts)
