"""One preflight for every live-data paper trial, whatever started it.

The CLI and the dashboard both start a trial, and they used to disagree about what a trial
needs. `event-arb trial` with no watched pairs fell back to venue-wide discovery and
generated pairs from whatever came back; with no approved pair it printed a note and ran
anyway, paper-trading nothing; with an unachievable freshness budget it logged a warning
that scrolled past, after which every scan quietly rejected its books and the run reported
an absence of opportunities that was really an absence of usable data.

None of those are safe on live data, and none of them were checks — they were prose. This
module makes them a single gate that both entry points run, and that fails **closed**.

Three principles shape what is a block and what is a warning:

- A live-data trial must never widen its own scope. No explicit watched pairs is a block,
  not a prompt to discover the venue.
- Not trading is a legitimate thing to do, but it has to be *chosen*. Observation-only is
  an explicit mode, so "no approved pairs" is a block for a trading run and expected for
  an observing one.
- Timing checks use measurement, not a pessimistic startup estimate. The estimate is a
  fallback for before any refresh has happened; refusing a workable configuration on it
  would push an operator toward raising ``max_book_age_ms``, which is the one change that
  makes stale prices invisible instead of visible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from event_arb.config import DataSource, Settings
from event_arb.domain.enums import MatchStatus
from event_arb.observability.logging import get_logger
from event_arb.orderbooks.coverage import RefreshOutcome
from event_arb.paper.portfolio import expected_accounts
from event_arb.persistence.database import Database
from event_arb.persistence.schema import inspect_schema
from event_arb.scanner import Scanner

__all__ = [
    "Finding",
    "PreflightReport",
    "PreflightStage",
    "Severity",
    "TrialMode",
    "run_preflight",
    "run_static_preflight",
]

log = get_logger(__name__)


class Severity(StrEnum):
    BLOCK = "block"
    WARN = "warn"
    INFO = "info"


class PreflightStage(StrEnum):
    """Which half of the gate a report covers.

    The split exists because the checks have different costs. ``STATIC`` reads only
    configuration and the database's own schema table, so it can run before the process is
    allowed to touch a venue — which is where a scope check has to run to mean anything.
    ``FULL`` adds the checks that need discovered markets, a restored portfolio, and one
    measured refresh.
    """

    STATIC = "static"
    FULL = "full"


class TrialMode(StrEnum):
    """What the operator asked this run to do.

    ``OBSERVATION_ONLY`` is a first-class mode rather than the accidental result of having
    nothing approved. A run that observes because it was told to is evidence about market
    behaviour; a run that observes because its configuration was wrong is a silent failure
    wearing the same output.
    """

    PAPER_TRADING = "paper_trading"
    OBSERVATION_ONLY = "observation_only"


@dataclass(frozen=True)
class Finding:
    severity: Severity
    code: str
    message: str
    remedy: str = ""

    def render(self) -> str:
        mark = {Severity.BLOCK: "BLOCK", Severity.WARN: "WARN ", Severity.INFO: "ok   "}[
            self.severity
        ]
        line = f"  [{mark}] {self.code}: {self.message}"
        return f"{line}\n           -> {self.remedy}" if self.remedy else line

    def as_dict(self) -> dict[str, object]:
        return {
            "severity": self.severity.value,
            "code": self.code,
            "message": self.message,
            "remedy": self.remedy,
        }


@dataclass
class PreflightReport:
    mode: TrialMode
    findings: list[Finding] = field(default_factory=list)
    stage: PreflightStage = PreflightStage.FULL

    def add(self, severity: Severity, code: str, message: str, remedy: str = "") -> None:
        self.findings.append(Finding(severity, code, message, remedy))

    @property
    def blocks(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.BLOCK]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.WARN]

    @property
    def passed(self) -> bool:
        return not self.blocks

    def render(self) -> str:
        stage = "" if self.stage is PreflightStage.FULL else f" ({self.stage.value})"
        header = f"  trial preflight{stage} — mode: {self.mode.value}"
        body = "\n".join(finding.render() for finding in self.findings)
        failed = f"  PREFLIGHT FAILED — {len(self.blocks)} blocking finding(s); "
        failed += (
            "no venue request was made"
            if self.stage is PreflightStage.STATIC
            else "nothing was started"
        )
        verdict = "  PREFLIGHT PASSED" if self.passed else failed
        return f"{header}\n{body}\n{verdict}"

    def as_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode.value,
            "stage": self.stage.value,
            "passed": self.passed,
            "blocking": len(self.blocks),
            "warnings": len(self.warnings),
            "findings": [f.as_dict() for f in self.findings],
        }


async def run_static_preflight(
    settings: Settings,
    database: Database | None,
    *,
    observation_only: bool = False,
) -> PreflightReport:
    """The half of the gate that must pass **before any venue request**.

    Scope is the reason this exists separately. Refusing an unscoped live trial after
    discovery has already run is not a scope check — the venue-wide ``GET /markets`` the
    check exists to prevent has happened by then. Both entry points used to call the whole
    preflight after ``Scanner.discover()``, so a live run with no watched pairs made
    exactly the request it was about to be refused for.

    Schema is checked here too, and for the same reason: reading tables that the current
    revision added should not be attempted against a database that predates them.
    """
    mode = TrialMode.OBSERVATION_ONLY if observation_only else TrialMode.PAPER_TRADING
    report = PreflightReport(mode=mode, stage=PreflightStage.STATIC)
    live = settings.data_source is DataSource.LIVE

    _check_safety_posture(report, settings)
    _check_ingestion_mode(report, settings)
    _check_watched_scope(report, settings, live=live)
    await _check_schema(report, database, live=live)

    log.info(
        "trial.preflight",
        stage=report.stage.value,
        mode=mode.value,
        passed=report.passed,
        blocking=[f.code for f in report.blocks],
        warnings=[f.code for f in report.warnings],
    )
    return report


async def run_preflight(
    scanner: Scanner,
    settings: Settings,
    *,
    observation_only: bool = False,
    measure_refresh: bool = True,
    static: PreflightReport | None = None,
) -> PreflightReport:
    """Decide whether this trial may start, and in what mode.

    ``measure_refresh`` issues one refresh so the freshness budget is judged on what
    refreshing actually costs. It is the point of the check; disable it only in tests that
    have already primed the timing.

    Pass ``static`` to carry forward a report from :func:`run_static_preflight` instead of
    repeating those checks, so one combined report reaches the operator. Callers that omit
    it still get the complete gate — the static checks are cheap and idempotent — but they
    will have run them too late to prevent an out-of-scope request.
    """
    mode = TrialMode.OBSERVATION_ONLY if observation_only else TrialMode.PAPER_TRADING
    report = PreflightReport(mode=mode)
    live = settings.data_source is DataSource.LIVE

    if static is None:
        _check_safety_posture(report, settings)
        _check_ingestion_mode(report, settings)
        _check_watched_scope(report, settings, live=live)
        await _check_schema(report, scanner.database, live=live)
    else:
        report.findings.extend(static.findings)

    approved = _check_pair_readiness(report, scanner, mode=mode)
    _check_paper_accounts(report, scanner, settings, mode=mode)
    _check_portfolio_restore(report, scanner, mode=mode)
    await _check_freshness(report, scanner, settings, measure=measure_refresh, mode=mode)

    log.info(
        "trial.preflight",
        stage=report.stage.value,
        mode=mode.value,
        passed=report.passed,
        blocking=[f.code for f in report.blocks],
        warnings=[f.code for f in report.warnings],
        approved_pairs=approved,
    )
    return report


# --------------------------------------------------------------------------- checks


def _check_safety_posture(report: PreflightReport, settings: Settings) -> None:
    """The invariant everything else rests on: this build cannot place an order."""
    if settings.live_trading_possible:
        report.add(
            Severity.BLOCK,
            "live_trading_possible",
            "live_trading_possible is true, which this build must never report",
            "do not run this build; the only execution adapter must refuse unconditionally",
        )
        return
    report.add(
        Severity.INFO,
        "live_trading_possible",
        "false — the only execution adapter refuses every submission",
    )


def _check_ingestion_mode(report: PreflightReport, settings: Settings) -> None:
    try:
        settings.validate_live_market_data()
    except ValueError as exc:
        report.add(
            Severity.BLOCK,
            "ingestion_mode",
            str(exc),
            "set EVENT_ARB_INGESTION_MODE=rest_polling for the credential-free path",
        )
        return
    if settings.data_source is DataSource.LIVE:
        report.add(
            Severity.INFO,
            "ingestion_mode",
            f"{settings.ingestion_mode.value} on polymarket {settings.polymarket.product.value}",
        )


def _check_watched_scope(report: PreflightReport, settings: Settings, *, live: bool) -> None:
    """A live trial names its markets. It does not discover them.

    Leaving ``watched_pairs`` empty is the single configuration that turns a bounded trial
    into a venue-wide listing: discovery falls through to an unbounded ``GET /markets`` and
    the trial then generates pairs from whatever came back. Kalshi Developer Agreement §3
    ties data access to markets you intend to trade, and pagination is unimplemented, so
    the result is both overbroad and silently truncated.
    """
    count = len(settings.trial.watched_pairs)
    if count:
        report.add(
            Severity.INFO,
            "watched_scope",
            f"{count} explicitly watched pair(s); discovery is scoped to their markets",
        )
        return
    if live:
        report.add(
            Severity.BLOCK,
            "watched_scope",
            "no trial.watched_pairs configured, so a live run would discover the whole "
            "venue and generate pairs from the result",
            "set EVENT_ARB_TRIAL__WATCHED_PAIRS to the pair(s) a human approved",
        )
        return
    report.add(
        Severity.WARN,
        "watched_scope",
        "no watched pairs; the fixture corpus will be used in full",
        "fixture runs may do this; a live run may not",
    )


def _check_pair_readiness(report: PreflightReport, scanner: Scanner, *, mode: TrialMode) -> int:
    approved = [p for p in scanner.state.pairs.values() if p.is_tradable_pair]
    suspended = [p for p in scanner.state.pairs.values() if p.status is MatchStatus.SUSPENDED]
    if suspended:
        report.add(
            Severity.WARN,
            "suspended_pairs",
            f"{len(suspended)} approval(s) suspended: "
            + ", ".join(p.id[:12] for p in suspended[:5]),
            "a rulebook, close time or settlement field changed; re-review before relying "
            "on these pairs. They cannot be traded while suspended",
        )
    if approved:
        report.add(
            Severity.INFO,
            "pair_readiness",
            f"{len(approved)} approved pair(s) available to trade",
        )
        return len(approved)
    if mode is TrialMode.OBSERVATION_ONLY:
        report.add(
            Severity.INFO,
            "pair_readiness",
            "no approved pairs, which is expected in observation-only mode",
        )
        return 0
    report.add(
        Severity.BLOCK,
        "pair_readiness",
        "no approved pair, so a paper-trading run would execute nothing and report an "
        "empty trial as a completed one",
        "approve a pair through the dashboard after a human reads both rulebooks, or "
        "start with --observation-only to record market behaviour without trading. "
        "Watching a pair is not approving it",
    )
    return 0


async def _check_schema(report: PreflightReport, database: Database | None, *, live: bool) -> None:
    """``create_all()`` creates missing tables; it does not migrate existing ones."""
    if database is None:
        report.add(
            Severity.WARN,
            "database_schema",
            "no database attached; nothing this run does will survive a restart",
            "set EVENT_ARB_DATABASE_URL for any run whose results matter",
        )
        return
    state = await inspect_schema(database)
    if state.is_current:
        report.add(Severity.INFO, "database_schema", f"at expected revision {state.expected}")
        return
    severity = Severity.BLOCK if live else Severity.WARN
    report.add(
        severity,
        "database_schema",
        state.problem or "database schema could not be verified",
        "alembic upgrade head, against EVENT_ARB_DATABASE_URL",
    )


def _check_paper_accounts(
    report: PreflightReport, scanner: Scanner, settings: Settings, *, mode: TrialMode
) -> None:
    """Every account the trial will draw on has to hold money before it starts."""
    accounts = expected_accounts(scanner.polymarket_product)
    floor = settings.paper.min_account_balance
    unfunded = [a for a in accounts if scanner.portfolio.available(a) <= floor]
    funded = ", ".join(f"{a.key}={scanner.portfolio.available(a)}" for a in accounts)
    if unfunded and mode is TrialMode.PAPER_TRADING:
        report.add(
            Severity.BLOCK,
            "paper_accounts",
            "no spendable balance in " + ", ".join(a.key for a in unfunded),
            "set EVENT_ARB_PAPER__STARTING_BALANCES per account, e.g. "
            '\'{"kalshi:USD":"10000","polymarket_us:USD":"10000"}\'. The per-asset '
            "settings open *each* account in that asset at that figure; they are not a "
            "pool being divided",
        )
    else:
        report.add(Severity.INFO, "paper_accounts", funded)

    limits = settings.paper
    report.add(
        Severity.INFO,
        "paper_limits",
        f"max_trade_notional={limits.max_trade_notional}, "
        f"max_open_notional={limits.max_open_notional}, "
        f"max_open_unhedged_quantity={limits.max_open_unhedged_quantity}",
    )


def _check_portfolio_restore(report: PreflightReport, scanner: Scanner, *, mode: TrialMode) -> None:
    """A partly-read portfolio understates exposure, so trading on it is refused."""
    restored = scanner.last_portfolio_restore
    if restored is None:
        if scanner.database is not None:
            report.add(
                Severity.WARN,
                "portfolio_restore",
                "portfolio was not restored before preflight",
                "call Scanner.restore() first, or balances start from configuration while "
                "stored trades already spent them",
            )
        return
    if restored.is_complete:
        report.add(
            Severity.INFO,
            "portfolio_restore",
            f"{restored.trades_restored} trade(s), "
            f"{restored.ledger_entries_applied} ledger entr(ies), open unhedged "
            f"{restored.open_unhedged_quantity}",
        )
        return
    severity = Severity.BLOCK if mode is TrialMode.PAPER_TRADING else Severity.WARN
    report.add(
        severity,
        "portfolio_restore",
        f"{len(restored.trades_unreadable)} stored trade(s) could not be rebuilt, so "
        "simulated exposure may be understated",
        "inspect the paper_trades rows named in the log before trading against this database",
    )


def _check_feed_coverage(
    report: PreflightReport,
    scanner: Scanner,
    outcome: RefreshOutcome,
    *,
    mode: TrialMode,
) -> None:
    """Judge the measured refresh on what it *fetched*, not on how fast it returned.

    The freshness budget below asks how long a batch took. That is a real question, but it
    is the wrong one to ask first: a batch in which every single request raised returns
    almost instantly, comfortably inside any budget, and used to pass this gate outright.
    Preflight would then hand a trial a feed that could not deliver one book, and the run
    reported an absence of opportunities rather than an absence of data.

    So coverage is checked before timing, and against the markets the trial is *required*
    to price. A market with no usable book is a block for a trading run: there is nothing
    to trade on and no way to tell a missed edge from a missing quote. Observation-only
    runs warn instead — watching a feed fail is a legitimate thing to observe, as long as
    the report says that is what happened.
    """
    if not outcome.required:
        # Scope is _check_watched_scope's job; having nothing to refresh is not a
        # refresh failure, and reporting it twice would just move the real block.
        return

    if outcome.is_complete:
        report.add(
            Severity.INFO,
            "feed_coverage",
            f"{len(outcome.covered)}/{len(outcome.required)} required market(s) returned a "
            f"usable book in {outcome.requests} request(s)",
        )
    else:
        severity = Severity.BLOCK if mode is TrialMode.PAPER_TRADING else Severity.WARN
        report.add(
            severity,
            "feed_coverage",
            f"{len(outcome.missing)} of {len(outcome.required)} required market(s) have no "
            f"usable book — {outcome.reason()}",
            "fix ingestion before starting a trial; a run that cannot fetch these books "
            "records an empty market rather than an outage",
        )

    # Coverage says a book arrived. Freshness says it arrived recently enough to price.
    # A trial needs both, and the store is the thing the scan will actually read.
    now = datetime.now(UTC)
    stale = []
    for key in outcome.covered:
        book, verdict = scanner.store.evaluate(key, now)
        if book is None or verdict is None or not verdict.is_fresh:
            stale.append(f"{key[0].value}:{key[2]}")
    if stale:
        severity = Severity.BLOCK if mode is TrialMode.PAPER_TRADING else Severity.WARN
        report.add(
            severity,
            "book_staleness",
            f"{len(stale)} freshly fetched book(s) are already stale: {', '.join(sorted(stale))}",
            "the books cannot be refreshed faster than they expire; shrink the batch or "
            "shorten the cycle rather than raising max_book_age_ms",
        )


async def _check_freshness(
    report: PreflightReport,
    scanner: Scanner,
    settings: Settings,
    *,
    measure: bool,
    mode: TrialMode,
) -> None:
    """Judge the freshness budget on a measured refresh, not a startup guess.

    The pre-refresh estimate assumes ``assumed_request_seconds`` per market serially, which
    is pessimistic for single-market book fetches. Blocking on it would make raising
    ``max_book_age_ms`` look like the fix, and raising that bound is how genuinely stale
    prices stop being rejected. So one real refresh is performed and the measurement is
    what decides.
    """
    measured: float | None = None
    if measure:
        try:
            outcome = await scanner.refresh_books()
        except Exception as exc:
            report.add(
                Severity.BLOCK,
                "freshness_budget",
                f"the preflight refresh failed, so no book could be fetched: {exc}",
                "fix ingestion before starting a trial; a run that cannot refresh reports "
                "an empty market rather than an error",
            )
            return
        measured = scanner.refresh_timing.worst_batch_seconds
        _check_feed_coverage(report, scanner, outcome, mode=mode)

    warning = settings.validate_freshness_budget(measured)
    if warning is None:
        basis = f"measured {measured:.2f}s batch" if measured is not None else "estimate"
        report.add(
            Severity.INFO,
            "freshness_budget",
            f"max_book_age_ms={settings.opportunity.max_book_age_ms} is achievable ({basis})",
        )
        return
    if measured is None:
        report.add(
            Severity.WARN,
            "freshness_budget",
            warning,
            "this is the pre-refresh estimate; re-run the preflight with measurement "
            "before changing the bound",
        )
        return
    report.add(
        Severity.BLOCK,
        "freshness_budget",
        warning,
        "shrink the refresh batch by watching fewer pairs, or shorten the cycle. Raising "
        "max_book_age_ms trades a visible failure for an invisible one: every scan would "
        "accept books it should have rejected",
    )
