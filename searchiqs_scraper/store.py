from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import secrets
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator

from .dates import SITE_TZ_NAME, DateRange, freeze_run_now, is_exact_partition, task_range
from .grouping import group_records
from .models import (
    OUTPUT_COLUMNS,
    PARSER_VERSION,
    CountUnit,
    Outcome,
    Record,
    WindowStatus,
    canonical_json,
)
from .sheet_tables import SheetTables, to_sheet_tables

SCHEMA_VERSION = 1
RUN_ID_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z_-]{0,63}$")
KEY_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})$")
DATE_SPLIT = "date"

GUARANTEE = (
    "Each COMPLETE window was observed twice with the same total and the same multiset of grid-row "
    "fingerprints, matching the total in the verified counting unit. This shows the observations agree; "
    "it does not prove the source was unchanged between or after them. Detail-page values are observed once."
)


class StoreError(Exception):
    pass


class RunLocked(StoreError):
    pass


class InventoryCorrupt(StoreError):
    pass


def new_run_id(run_now: datetime) -> str:
    return f"{freeze_run_now(run_now):%Y%m%dT%H%M%S}-{secrets.token_hex(3)}"


def window_key(r: DateRange) -> str:
    return f"{r.start.isoformat()}_{r.end.isoformat()}"


def parse_window_key(key: str) -> DateRange:
    match = KEY_RE.fullmatch(key)
    if not match:
        raise ValueError(f"not a window key: {key!r}")
    rng = DateRange(date.fromisoformat(match[1]), date.fromisoformat(match[2]))
    if window_key(rng) != key:
        raise ValueError(f"non-canonical window key: {key!r}")
    return rng


def _digest(obj: dict[str, Any]) -> str:
    body = {k: v for k, v in obj.items() if k != "sha256"}
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def seal(obj: dict[str, Any]) -> dict[str, Any]:
    return {**obj, "sha256": _digest(obj)}


def is_sealed(obj: Any) -> bool:
    return isinstance(obj, dict) and obj.get("sha256") == _digest(obj)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise


def json_bytes(obj: Any) -> bytes:
    text = json.dumps(obj, sort_keys=True, ensure_ascii=False, indent=1, allow_nan=False)
    return (text + "\n").encode("utf-8")


class FileLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh: Any = None

    def acquire(self) -> None:
        fh = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            raise RunLocked(f"{self.path} is locked by another process") from None
        self._fh = fh

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None


@dataclass(frozen=True)
class RunMeta:
    run_id: str
    run_now: datetime
    egress_country: str | None
    count_unit: CountUnit
    config: dict[str, Any]
    parser_version: int = PARSER_VERSION

    def __post_init__(self) -> None:
        if not RUN_ID_RE.fullmatch(self.run_id):
            raise ValueError(f"invalid run id {self.run_id!r}")
        object.__setattr__(self, "run_now", freeze_run_now(self.run_now))
        object.__setattr__(self, "count_unit", CountUnit(self.count_unit))

    @property
    def range(self) -> DateRange:
        return task_range(self.run_now)

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA_VERSION,
            "run_id": self.run_id,
            "run_now": self.run_now.isoformat(),
            "timezone": SITE_TZ_NAME,
            "date_from": self.range.start.isoformat(),
            "date_to": self.range.end.isoformat(),
            "egress_country": self.egress_country,
            "count_unit": self.count_unit.value,
            "parser_version": self.parser_version,
            "config": self.config,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> RunMeta:
        if data.get("schema") != SCHEMA_VERSION or data.get("timezone") != SITE_TZ_NAME:
            raise ValueError("unsupported run.json schema or timezone")
        meta = cls(
            run_id=data["run_id"],
            run_now=datetime.fromisoformat(data["run_now"]),
            egress_country=data["egress_country"],
            count_unit=CountUnit(data["count_unit"]),
            config=data["config"],
            parser_version=data["parser_version"],
        )
        if (data["date_from"], data["date_to"]) != (meta.range.start.isoformat(), meta.range.end.isoformat()):
            raise ValueError("run.json date range does not match its frozen run_now")
        return meta


@dataclass
class WindowNode:
    range: DateRange
    status: WindowStatus
    parent: str | None
    attempt: int = 1
    total: int | None = None
    counts: dict[str, int] = field(default_factory=dict)
    reason: str | None = None
    rows: list[dict[str, Any]] = field(default_factory=list)
    records: list[Record] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    children: list[str] = field(default_factory=list)
    split_kind: str | None = None
    parser_version: int = PARSER_VERSION

    def __post_init__(self) -> None:
        self.status = WindowStatus(self.status)
        if self.attempt < 1:
            raise ValueError("attempt must be >= 1")
        if self.status is WindowStatus.SPLIT:
            if self.rows or self.records:
                raise ValueError("a SPLIT node holds no rows or records")
            if not self.children or self.split_kind is None:
                raise ValueError("a SPLIT node needs children and a split kind")
        elif self.children or self.split_kind is not None:
            raise ValueError(f"a {self.status} node has no children")
        if self.status is WindowStatus.INCOMPLETE and not self.reason:
            raise ValueError("an INCOMPLETE node needs a reason")

    @property
    def key(self) -> str:
        return window_key(self.range)

    def to_json(self, run_id: str) -> dict[str, Any]:
        return {
            "schema": SCHEMA_VERSION,
            "run_id": run_id,
            "key": self.key,
            "window": {"from": self.range.start.isoformat(), "to": self.range.end.isoformat()},
            "parent": self.parent,
            "attempt": self.attempt,
            "status": self.status.value,
            "T": self.total,
            "counts": self.counts,
            "reason": self.reason,
            "rows": self.rows,
            "records": [r.to_json() for r in self.records],
            "issues": self.issues,
            "children": self.children,
            "split_kind": self.split_kind,
            "parser_version": self.parser_version,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> WindowNode:
        if data.get("schema") != SCHEMA_VERSION:
            raise ValueError("unsupported node schema")
        rng = DateRange(date.fromisoformat(data["window"]["from"]), date.fromisoformat(data["window"]["to"]))
        if data["key"] != window_key(rng):
            raise ValueError("node key does not match its window")
        return cls(
            range=rng,
            status=WindowStatus(data["status"]),
            parent=data["parent"],
            attempt=data["attempt"],
            total=data["T"],
            counts=data["counts"],
            reason=data["reason"],
            rows=data["rows"],
            records=[Record.from_json(r) for r in data["records"]],
            issues=list(data["issues"]),
            children=list(data["children"]),
            split_kind=data["split_kind"],
            parser_version=data["parser_version"],
        )


def split_children(node: WindowNode) -> list[DateRange]:
    if node.split_kind != DATE_SPLIT:
        raise ValueError(f"unsupported split kind {node.split_kind!r}")
    if len(node.children) < 2 or len(set(node.children)) != len(node.children):
        raise ValueError("a split needs at least two distinct children")
    ranges = [parse_window_key(k) for k in node.children]
    if not is_exact_partition(node.range, ranges):
        raise ValueError("children do not exactly partition the parent range")
    return sorted(ranges)


@dataclass(frozen=True)
class Leaf:
    key: str
    range: DateRange
    parent: str | None
    node: WindowNode | None
    problem: str | None

    @property
    def covered(self) -> bool:
        return self.node is not None and self.node.status is WindowStatus.COMPLETE


@dataclass(frozen=True)
class Resolution:
    leaves: tuple[Leaf, ...]
    reached: frozenset[str]
    orphans: tuple[str, ...]

    @property
    def gaps(self) -> list[Leaf]:
        return [leaf for leaf in self.leaves if not leaf.covered]

    @property
    def complete(self) -> bool:
        return not self.gaps


class RunStore:
    def __init__(self, output_dir: Path, run_id: str) -> None:
        if not RUN_ID_RE.fullmatch(run_id):
            raise StoreError(f"invalid run id {run_id!r}")
        self.run_id = run_id
        self.dir = Path(output_dir) / run_id
        self.windows_dir = self.dir / "windows"
        self.export_dir = self.dir / "export"
        self.run_json = self.dir / "run.json"
        self._lock: FileLock | None = None

    @contextmanager
    def locked(self, create: bool = False) -> Iterator[RunStore]:
        if create:
            self.windows_dir.mkdir(parents=True, exist_ok=True)
        elif not self.dir.is_dir():
            raise StoreError(f"no run directory for {self.run_id}")
        lock = FileLock(self.dir / ".lock")
        lock.acquire()
        self._lock = lock
        try:
            yield self
        finally:
            self._lock = None
            lock.release()

    def _require_lock(self) -> None:
        if self._lock is None:
            raise StoreError("run directory must be locked before it is modified")

    def create(self, meta: RunMeta) -> None:
        self._require_lock()
        if meta.run_id != self.run_id:
            raise StoreError("run id mismatch")
        if self.run_json.exists():
            raise StoreError(f"run {self.run_id} already exists")
        atomic_write_bytes(self.run_json, json_bytes(seal(meta.to_json())))

    def load_run(self) -> RunMeta:
        try:
            data = json.loads(self.run_json.read_text(encoding="utf-8"))
            if not is_sealed(data):
                raise ValueError("checksum mismatch")
            meta = RunMeta.from_json(data)
        except FileNotFoundError:
            raise StoreError(f"run.json missing for {self.run_id}") from None
        except (ValueError, KeyError, TypeError) as exc:
            raise StoreError(f"run.json corrupt for {self.run_id}: {exc}") from None
        if meta.run_id != self.run_id:
            raise StoreError("run.json belongs to a different run")
        return meta

    def cleanup_tmp(self) -> list[Path]:
        self._require_lock()
        removed = []
        for directory in (self.dir, self.windows_dir, self.export_dir):
            for tmp in directory.glob("*.tmp") if directory.is_dir() else []:
                tmp.unlink()
                removed.append(tmp)
        return removed

    def commit(self, meta: RunMeta, node: WindowNode) -> None:
        self._require_lock()
        if not meta.range.contains(node.range):
            raise StoreError(f"window {node.range} is outside the run range {meta.range}")
        if node.parser_version != meta.parser_version:
            raise StoreError("node parser version differs from the run")
        if (node.parent is None) != (node.range == meta.range):
            raise StoreError("only the root window may have no parent")
        if node.status is WindowStatus.SPLIT:
            split_children(node)
        self.windows_dir.mkdir(parents=True, exist_ok=True)
        path = self.windows_dir / f"{node.key}.json"
        atomic_write_bytes(path, json_bytes(seal(node.to_json(meta.run_id))))

    def read_node(self, meta: RunMeta, key: str, expected_parent: str | None) -> tuple[WindowNode | None, str | None]:
        path = self.windows_dir / f"{key}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None, "missing"
        except (OSError, ValueError) as exc:
            return None, f"invalid: unreadable ({type(exc).__name__})"
        if not is_sealed(data):
            return None, "invalid: checksum mismatch"
        try:
            node = WindowNode.from_json(data)
        except (ValueError, KeyError, TypeError) as exc:
            return None, f"invalid: {exc}"
        if data.get("run_id") != meta.run_id:
            return None, "invalid: belongs to another run"
        if node.key != key:
            return None, "invalid: key mismatch"
        if node.parent != expected_parent:
            return None, "invalid: parent mismatch"
        if node.parser_version != meta.parser_version:
            return None, "invalid: parser version mismatch"
        return node, None

    def resolve(self, meta: RunMeta) -> Resolution:
        stack: list[tuple[str, DateRange, str | None]] = [(window_key(meta.range), meta.range, None)]
        reached: set[str] = set()
        leaves: list[Leaf] = []
        while stack:
            key, rng, parent = stack.pop()
            if key in reached:
                raise InventoryCorrupt(f"window {key} is reached twice")
            reached.add(key)
            node, problem = self.read_node(meta, key, parent)
            if node is None:
                leaves.append(Leaf(key, rng, parent, None, problem))
            elif node.status is WindowStatus.SPLIT:
                try:
                    children = split_children(node)
                except ValueError as exc:
                    raise InventoryCorrupt(f"window {key}: {exc}") from None
                stack.extend((window_key(c), c, key) for c in reversed(children))
            else:
                problem = None if node.status is WindowStatus.COMPLETE else f"incomplete: {node.reason}"
                leaves.append(Leaf(key, rng, parent, node, problem))
        files = self.windows_dir.glob("*.json") if self.windows_dir.is_dir() else []
        orphans = sorted(p.name for p in files if p.stem not in reached)
        return Resolution(tuple(sorted(leaves, key=lambda leaf: leaf.range)), frozenset(reached), tuple(orphans))

    def move_orphans(self, resolution: Resolution) -> list[Path]:
        self._require_lock()
        target_dir = self.windows_dir / "orphaned"
        moved = []
        for name in resolution.orphans:
            source = self.windows_dir / name
            if not source.exists():
                continue
            target_dir.mkdir(exist_ok=True)
            target = target_dir / name
            n = 1
            while target.exists():
                target = target_dir / f"{name}.{n}"
                n += 1
            os.replace(source, target)
            moved.append(target)
        return moved

    def write_export(self, export: ExportData) -> None:
        self._require_lock()
        self.export_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(self.export_dir / "records.csv", export.csv_bytes())
        atomic_write_bytes(self.export_dir / "sheet_rows.json", json_bytes(export.tables.to_json()))
        atomic_write_bytes(self.export_dir / "report.json", json_bytes(export.report))


@dataclass(frozen=True)
class ExportData:
    outcome: Outcome
    records: tuple[Record, ...]
    tables: SheetTables
    report: dict[str, Any]

    def csv_bytes(self) -> bytes:
        buf = io.StringIO(newline="")
        writer = csv.writer(buf, lineterminator="\r\n")
        writer.writerow(OUTPUT_COLUMNS)
        writer.writerows(r.to_row() for r in self.records)
        return ("﻿" + buf.getvalue()).encode("utf-8")


def build_export(meta: RunMeta, resolution: Resolution, stopped: str | None = None) -> ExportData:
    records: list[Record] = []
    for leaf in resolution.leaves:
        if leaf.node is None:
            continue
        for record in leaf.node.records:
            if leaf.node.status is WindowStatus.INCOMPLETE:
                record = replace(record, issues=record.issues + (f"window {leaf.range} {leaf.problem}",))
            records.append(record)
    records.sort(key=Record.sort_key)
    grouped = group_records(records)
    records = sorted(grouped.records, key=Record.sort_key)

    uncovered = [f"{leaf.range} ({leaf.problem})" for leaf in resolution.gaps]
    problem_records = sum(1 for r in records if r.has_problems)
    outcome = Outcome.COMPLETE if resolution.complete and not problem_records else Outcome.INCOMPLETE

    run_info = [
        ("Run ID", meta.run_id),
        ("Scrape outcome", outcome.value),
        ("Date range", f"{meta.range.start.isoformat()} to {meta.range.end.isoformat()} (inclusive)"),
        ("Run time (America/New_York)", meta.run_now.isoformat()),
        ("Counting unit", meta.count_unit.value),
        ("Records", str(len(records))),
        ("Records with issues", str(problem_records)),
        ("Uncovered windows", str(len(uncovered))),
        ("Duplicate documents removed", str(grouped.duplicates_removed)),
        ("Identity conflicts", str(len(grouped.conflicts))),
        ("Stopped early", stopped or "no"),
        ("Parser version", str(meta.parser_version)),
        ("Guarantee", GUARANTEE),
    ]
    tables = to_sheet_tables(OUTPUT_COLUMNS, [r.to_row() for r in records], run_info)
    report = {
        "run_id": meta.run_id,
        "scrape_outcome": outcome.value,
        "date_from": meta.range.start.isoformat(),
        "date_to": meta.range.end.isoformat(),
        "count_unit": meta.count_unit.value,
        "leaves": len(resolution.leaves),
        "records": len(records),
        "records_with_issues": problem_records,
        "uncovered": uncovered,
        "duplicates_removed": grouped.duplicates_removed,
        "identity_conflicts": list(grouped.conflicts),
        "stopped": stopped,
        "orphans": list(resolution.orphans),
        "export_fidelity": tables.fidelity,
        "content_sha256": tables.content_sha256(),
    }
    return ExportData(outcome, tuple(records), tables, report)
