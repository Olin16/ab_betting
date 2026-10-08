# Paper-trading readiness review — 2026-09-07

## Assessment

The fixture simulator and continuous runtime exist and pass the offline suite. A bounded
read-only schema smoke test is the next external validation step. A sustained paper trial
also needs accounting and recovery fixes: the earlier readiness document overstates
completion of those parts.

This review inspected the existing graph, current source, tests, operational documents,
and the local SQLite database in read-only mode. Reproductions used fixture data and a
temporary database. No live venue request, order, approval, or trial was started. Current
venue terms, account eligibility, and external API behavior were not independently checked.

## Priority 1 — accurate paper balances and capital limits

`src/event_arb/paper/portfolio.py:148` hardcodes leg 2 to USDC, although Polymarket US
opportunities correctly carry USD as their settlement asset. `Scanner.paper_trade`
(`src/event_arb/scanner.py:487`) executes and records without calling `has_capacity` or
enforcing portfolio exposure limits. The controls under `execution/` are separate future
live-execution scaffolding and are not connected to this path.

Reproduced with a US fixture opportunity and zero starting balances: a 100-contract trade
finished `both_legs_filled`, leaving USD -48.7500 and USDC -39.4100. Both opportunity legs
were USD. Passing tests currently do not prevent this behavior.

Required work:

- Debit the asset carried by each leg; track available funds separately per venue/product
  so two USD accounts are not treated as transferable pooled cash.
- Reserve sufficient cash including fees before attempting a paper trade; reject or size
  down when funds are insufficient.
- Enforce paper-specific notional and open-exposure limits, including the response to a
  failed second leg. Preserve the disabled real-order path.
- Regression checks: US USD/USD, Global USD/USDC, zero funds, fee-inclusive capacity,
  repeated opportunities, and concurrent attempts cannot overspend.

## Priority 2 — restore the actual portfolio after restart

`Scanner.restore` (`src/event_arb/scanner.py:676`) loads markets and pairs, but does not
rehydrate paper trades, balances, or outstanding exposure. Every new scanner constructs a
fresh starting portfolio. `PaperTradeRepository` stores trade history but that history is
not used by restore.

Reproduced: database trade count 1, restored portfolio trade count 0, balances reset to
their configured starting values.

Required work: durable ledger or reconstructable positions, stable identifiers and
idempotency, recovery of balances/exposure/reporting, and restart tests after both a full
hedge and a failed hedge. Also specify recovery when interruption occurs between legs or
between a simulated fill and persistence.

## Priority 3 — connect settlement to cash and reporting

`paper/executor.py:373` and `paper/settlement.py:32` implement settlement calculations,
but there is no runtime/CLI/API settlement workflow calling them. Portfolio balances are
only debited. `PaperPortfolio.daily_report` always sums `realized_pnl_if_hedged`, which
assumes an unhedged remainder expires worthless, rather than consuming final settlement
P&L. Its hedge counts depend on the current trade status, so changing that to `settled`
also removes the previous fill classification from the counts.

Reproduced: a calculated settlement payout of 100.0000 did not change either cash balance.

Required work: a reviewed outcome-entry or outcome-ingestion workflow, per-venue outcome
and inversion handling, idempotent payout credits, persisted settlement updates, and
separate execution estimates versus settled P&L in reporting. Verify one complete
trade-to-settlement-to-restart lifecycle before relying on trial profitability reports.

## Priority 4 — preflight and storage for a bounded trial

The trial command proceeds without explicit watched pairs and then generates pairs from
discovery (`src/event_arb/main.py:290`). Freshness-budget failures warn rather than stop,
and no approved pair produces a warning followed by an idle paper run. Health checks in
the maintenance loop run every 300 seconds, longer than the one-minute degraded-feed
stop condition described in operations documentation.

Required work: a common CLI/dashboard preflight for live-data paper trials that checks
explicit market scope, approved pair readiness, database revision, and measured freshness;
make an observation-only run an explicit choice. Add a clear watchdog/stop policy for
persistent data or storage failures.

The inspected `data/event_arb.db` has zero market, pair, opportunity, and paper-trade rows.
Its Alembic revision is `3ea85fd4182a`; repository head is `2484faa40d3a`. Use a dedicated
trial database at the current schema. `create_all()` at trial startup does not migrate
existing tables.

## External validation and operator steps

The latest documented smoke attempt, 2026-08-26, stopped after two successful catalogue
requests because it found no candidates. No books were fetched. The scoring correction
has offline regression coverage, but it cannot establish that the two bounded live
catalogue pages now contain genuinely equivalent markets.

1. Re-run the documented bounded smoke test after an explicit operator decision. Its
   existing request ceiling is 31 unauthenticated GET requests; inspect the result before
   changing scope. See `smoke-test-plan.md` and `zero-candidate-diagnosis.md`.
2. Find at least one actual pair and have the operator review both full rulebooks,
   outcomes, dates, settlement authority/evidence, and current fees. Watching a pair does
   not approve it. Do not lower approval standards to produce trades.
3. Resolve the operator confirmations recorded in `trial-readiness.md` for the intended
   venues and account. The implemented public REST paper path does not require keys;
   authenticated WebSocket/live-execution work is not a prerequisite for that path.
4. Begin with one approved pair and a supervised, time-boxed REST trial. Verify request
   counts, refresh age, rejection reasons, simulated fills, balances, persistence, and
   evidence grades. Extend duration only once those results are sound.
5. Gather moved-book leg-2 observations before interpreting hedge success as evidence
   about changing markets. Fixtures are correctly labelled `unchanged_reread`.

Pagination, additional venues, sportsbook execution, and real-order adapters are not
needed to begin a one-pair paper trial. Document cleanup should accompany the fixes:
readiness currently calls persistence done, and operations has stale authentication and
other status descriptions.

## Validation performed

- Offline pytest: **896 passed, 5 deselected**, one third-party deprecation warning.
- Ruff lint: passed. Ruff format: all 159 files formatted.
- Application mypy: passed, 82 source files.
- Test mypy: passed, 53 source files.
- Fixture-only reproductions confirmed wrong-asset debit, missing capital enforcement,
  missing portfolio restore, and absence of settlement cash credits.

Graph query expansion used its vocabulary: paper, trial, readiness, settlement, executor,
portfolio, smoke, candidates, approval, freshness. Source inspection and reproductions
were used to validate and extend the graph's architectural pointers.

## Follow-up — 2026-09-08: the capital gate was inert on the path that runs unattended

Priority 1's work landed, and the suite passed, but the gate it added never refused
anything on the automated path. Two ids that were supposed to name the same hold did not:

- `Scanner.paper_trade_if_funded` reserves under `_reservation_id(opportunity)` —
  `hold:{dedup_key}:{detected_at}`.
- `PaperPortfolio.record(trade)` released under `trade.id`, a UUID minted at execution.

`release` recomputed the amount from the funding requirement and appended the entry
unconditionally, so it did not need a hold to exist in order to hand cash back. Every
automated trade therefore wrote a release for a hold nobody took, cancelling its own debit
in `available` to the cent: `reserved` went **negative**, and `available` stayed pinned at
the opening balance no matter how much had been spent.

Reproduced on the Global fixture corpus: one 100-contract trade costing $56.74 left
`reserved` at `-56.7400` and `available` back at exactly `10000.0000`. With the release
restored to its old form, an account funded for **two** trades took **four**.

The existing tests missed it because `test_a_partial_fill_releases_the_cash_it_did_not_spend`
reserves under `"trade-1"` and records a trade whose id is also `"trade-1"` — the one case
where the two ids agree — and because
`test_concurrent_attempts_cannot_overspend_one_account` covers only the concurrent race,
which the lock already guarded. The sequential case was untested.

Two further defects fell out of the same investigation:

- **Cleanup masked the error it was unwinding from.** The release write runs in a
  `finally`, so when stopping the runner cancelled a paper trade mid-write, the cancelled
  connection's `OperationalError` was raised *over* the `CancelledError`. The cycle loop's
  `except asyncio.CancelledError: raise` never saw it, and an orderly stop landed in the
  final trial report as an error with a spurious consecutive-failure count.
- **A hold could outlive the process that took it.** Retention deliberately never compacts
  an unreleased reservation, so a run killed between reserving and releasing stranded that
  cash permanently; each interrupted run would shrink usable capital again.

Fixes:

- `PaperLedger` gained `holds_for` / `outstanding_holds`, and `release(trade_id)` now
  derives both *which* accounts and *how much* from the hold itself. Releasing a hold that
  was never taken is a no-op, and `reserved` cannot go negative by construction.
- The cleanup write is wrapped so its failure is logged
  (`paper.reservation_release_unrecorded`) instead of replacing the exception in flight.
- `restore_portfolio` reaps every outstanding hold — a hold can only survive into a new
  process if the one that took it died — and `Scanner.restore_portfolio` persists the
  reaping so the next start does not re-read the same dead hold.

Regression checks added: sequential depletion through `paper_trade_if_funded`, the
differing-ids shape at portfolio level, releases as exact and single-use, cleanup-failure
masking (both `RuntimeError` and `CancelledError`), and the restart reaper. Each was
mutation-checked against the old behaviour.

## Validation performed — 2026-09-08

- Offline pytest: **988 passed, 5 deselected**, one third-party deprecation warning.
- Ruff lint: passed. Ruff format: 143 files formatted.
- Application mypy: passed, 85 source files.
- Test mypy: passed, 58 source files. (21 type errors in the in-progress paper tests,
  which predate this session, were fixed rather than left on the branch.)
