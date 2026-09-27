from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable

from .config import Config
from .dates import DateRange
from .flow import ChallengeError, FlowError, PageLimitReached, PassResult, SiteFlow
from .http import CurlTransport, DeadlineExceeded, EgressError, HttpClient, HttpError, Pacer, egress_country
from .models import CountUnit, WindowStatus
from .store import DATE_SPLIT, RunMeta, RunStore, WindowNode, window_key

log = logging.getLogger(__name__)

VERIFIED_COUNT_UNIT = CountUnit.DOCUMENTS
REQUIRED_COUNTRY = "US"
STOP_ERRORS = (ChallengeError, DeadlineExceeded, EgressError)

PassRunner = Callable[[DateRange], PassResult]
EgressCheck = Callable[[], str]


@dataclass(frozen=True)
class Verdict:
    status: WindowStatus
    reason: str | None = None
    split: bool = False


@dataclass(frozen=True)
class Observation:
    verdict: Verdict
    result: PassResult | None
    passes: int
    errors: tuple[str, ...]


@dataclass
class CollectResult:
    windows_committed: int = 0
    passes: int = 0
    stopped: str | None = None
    stop_code: str | None = None
    moved_orphans: list[str] = field(default_factory=list)


def passes_agree(a: PassResult, b: PassResult) -> bool:
    return a.total == b.total and Counter(r.fingerprint for r in a.rows) == Counter(r.fingerprint for r in b.rows)


def assess(result: PassResult, unit: CountUnit) -> Verdict:
    rows = len(result.rows)
    if unit is CountUnit.UNVERIFIED:
        return Verdict(WindowStatus.INCOMPLETE, "unresolved-count-unit: the site's counting unit is not verified")
    if unit is CountUnit.ROWS:
        collected = rows
    else:
        ids = [r.record.doc_id for r in result.rows]
        if None in ids:
            return Verdict(WindowStatus.INCOMPLETE, "unresolved-count-unit: rows without a document id")
        collected = len(set(ids))
        if collected != rows:
            return Verdict(WindowStatus.INCOMPLETE, f"duplicate document ids: {rows} rows, {collected} distinct")
    if collected == result.total:
        return Verdict(WindowStatus.COMPLETE)
    if collected < result.total:
        return Verdict(WindowStatus.INCOMPLETE, f"count shortfall: collected {collected} of {result.total}",
                       split=True)
    return Verdict(WindowStatus.INCOMPLETE, f"count excess: collected {collected}, site reports {result.total}")


class Collector:
    def __init__(self, store: RunStore, meta: RunMeta, run_pass: PassRunner, *, check_egress: EgressCheck,
                 max_passes: int = 6) -> None:
        if max_passes < 2:
            raise ValueError("max_passes must be at least 2")
        self.store = store
        self.meta = meta
        self.run_pass = run_pass
        self.check_egress = check_egress
        self.max_passes = max_passes

    def run(self) -> CollectResult:
        result = CollectResult()
        self.store.cleanup_tmp()
        resolution = self.store.resolve(self.meta)
        result.moved_orphans = [p.name for p in self.store.move_orphans(resolution)]
        for name in result.moved_orphans:
            log.warning("moved unreachable window file %s to windows/orphaned/", name)
        try:
            for leaf in resolution.gaps:
                attempt = leaf.node.attempt + 1 if leaf.node is not None else 1
                log.info("window %s: %s, collecting (attempt %d)", leaf.range, leaf.problem, attempt)
                self._window(leaf.range, leaf.parent, attempt, result)
        except STOP_ERRORS as exc:
            result.stopped, result.stop_code = f"{exc.code}: {exc}", exc.code
            log.error("run stopped: %s", result.stopped)
        return result

    def _window(self, rng: DateRange, parent: str | None, attempt: int, result: CollectResult) -> None:
        country = self.check_egress()
        if country != REQUIRED_COUNTRY:
            raise EgressError(f"egress country is {country}, expected {REQUIRED_COUNTRY}: check the VPN")
        obs = self._observe(rng)
        result.passes += obs.passes
        if obs.verdict.split and rng.days > 1:
            children = rng.split_half()
            node = WindowNode(rng, WindowStatus.SPLIT, parent, attempt=attempt,
                              total=obs.result.total if obs.result else None, counts=self._counts(obs),
                              reason=obs.verdict.reason, issues=list(obs.errors),
                              children=[window_key(c) for c in children], split_kind=DATE_SPLIT)
            self._commit(node, result)
            for child in children:
                self._window(child, node.key, 1, result)
            return
        rows = obs.result.rows if obs.result else ()
        node = WindowNode(
            rng, obs.verdict.status, parent, attempt=attempt,
            total=obs.result.total if obs.result else None, counts=self._counts(obs), reason=obs.verdict.reason,
            rows=[{"fingerprint": r.fingerprint, "doc_id": r.record.doc_id, "related_refs": list(r.related_refs)}
                  for r in rows],
            records=[r.record for r in rows], issues=list(obs.errors),
        )
        self._commit(node, result)

    def _observe(self, rng: DateRange) -> Observation:
        previous: PassResult | None = None
        errors: list[str] = []
        successes = 0
        for n in range(1, self.max_passes + 1):
            try:
                current = self.run_pass(rng)
            except PageLimitReached as exc:
                errors.append(exc.code)
                return Observation(Verdict(WindowStatus.INCOMPLETE, f"{exc.code}: {exc}", split=True),
                                   previous, n, tuple(errors))
            except STOP_ERRORS:
                raise
            except (FlowError, HttpError) as exc:
                errors.append(exc.code)
                log.warning("window %s: pass %d failed (%s: %s), retrying with a fresh session", rng, n, exc.code, exc)
                continue
            successes += 1
            log.info("window %s: pass %d, T=%d, %d rows", rng, n, current.total, len(current.rows))
            if previous is not None and passes_agree(previous, current):
                return Observation(assess(current, self.meta.count_unit), current, n, tuple(errors))
            if previous is not None:
                log.warning("window %s: pass %d disagrees with the previous pass", rng, n)
            previous = current
        if successes >= 2:
            reason = "unstable: consecutive passes disagreed"
        else:
            reason = f"pass errors: {', '.join(errors)}"
        return Observation(Verdict(WindowStatus.INCOMPLETE, reason), previous, self.max_passes, tuple(errors))

    @staticmethod
    def _counts(obs: Observation) -> dict[str, int]:
        rows = obs.result.rows if obs.result else ()
        return {"rows": len(rows), "doc_ids": len({r.record.doc_id for r in rows if r.record.doc_id}),
                "passes": obs.passes, "pass_errors": len(obs.errors)}

    def _commit(self, node: WindowNode, result: CollectResult) -> None:
        self.store.commit(self.meta, node)
        result.windows_committed += 1
        log.info("window %s: committed %s%s", node.range, node.status.value,
                 f" ({node.reason})" if node.reason else "")


def site_client(config: Config, *, deadline: float | None = None, pacer: Pacer | None = None) -> HttpClient:
    return HttpClient(CurlTransport(config), config, deadline=deadline, pacer=pacer)


def site_pass_runner(config: Config, *, deadline: float | None = None) -> PassRunner:
    pacer = Pacer()

    def run_pass(window: DateRange) -> PassResult:
        client = site_client(config, deadline=deadline, pacer=pacer)
        try:
            return SiteFlow(client, max_pages=config.max_pages).run_pass(window)
        finally:
            client.close()

    return run_pass


def site_egress_check(config: Config) -> EgressCheck:
    def check() -> str:
        transport = CurlTransport(config)
        try:
            return egress_country(transport, config)
        finally:
            transport.close()

    return check
