# Claude: finish engineering, then start the paper trial

## Review result — September 10, 2026

The current working tree is **not ready for an unattended live-data paper trial**.
Continue the existing implementation; do not rebuild it. Review baseline: branch
`smoke-test-preflight-blocked`, HEAD `c4f0cf0`, 34 modified/untracked paths before this
file. Git history alone excludes the substantial unfinished work. This review used
source, the September 8 graph, the imported Claude continuation, offline tests, and a
temporary-database failure reproduction. No live venue requests or orders were made.

The project already connects fixture/REST ingestion, normalization, reviewed matching,
depth/fee-aware detection, paper execution, accounts/ledger, restoration, settlement,
runtime supervision, persistence, and dashboard reporting. The next milestone is
reliability and reproducible operating evidence, not additional venues or features.

### What Claude has advanced

- Per-loop watchdog streaks, stop-before-audit behavior, task completion callbacks,
  runner wakeup and CLI failure status are implemented; their regressions pass.
- CLI/dashboard static gates precede discovery. Schema emptiness checks inspect all
  application tables rather than a single sentinel. Startup regressions pass.
- Settlement retries now return the full deterministic payout-entry set. Failed
  trade-row and ledger-write retry/restart regressions pass.
- Reservation persistence is inside cleanup protection; releases derive from actual
  holds. Reservation recovery regressions pass.
- Interrupted-execution status and partial-trade persistence are implemented, but
  their two new integration tests do not reach the intended interruption.
- CLI settlement now reads a stored pair for inversion and refuses unknown alignment.
  This needs a regression through the actual command, not only outcome helper tests.

### Fresh verification

| Check | Result |
| --- | --- |
| `.venv/bin/python -m pytest -p no:cacheprovider` | 1011 passed, 2 failed, 5 live tests deselected; 33.86 s |
| `.venv/bin/ruff check src tests` | 14 errors |
| `.venv/bin/ruff format --check src tests` | 4 files need formatting |
| `.venv/bin/mypy src` | Pass, 85 files |
| `.venv/bin/mypy --config-file mypy-tests.ini tests` | 3 errors in 2 files |
| `.venv/bin/event-arb doctor` | Fixture defaults; real-order adapter refusal verified |

The doctor result is not validation of a future live configuration. One third-party
Starlette deprecation warning remains. The existing `data/event_arb.db`, inspected
read-only, is at `3ea85fd4182a`; expected head is `6b2f1c9a7d40`.

## Next implementation steps, in order

### 1. Finish accounting durability before preparing a live run

**Reproduced blocker:** `Scanner.paper_trade` publishes a trade and cash debits to
memory before the database transaction succeeds. With `PaperTradeRepository.record`
patched to raise during a fixture funded trade, memory held one trade with balances
`kalshi:USD=9951.2500`, `polymarket_us:USD=9960.5900`. After closing and reopening the
same temporary database, it held zero trades and both balances were `10000.0000`.
The restore report still said `is_complete=True`.

Implement a consistent durable publication/recovery policy for completed and interrupted
attempts. A failed write must not silently erase simulated fills on restart or let the
run claim complete evidence. Cover transaction/commit failure, retry without duplicate
execution, and restart immediately after failure. If a simulated attempt is invalidated
instead of recovered, explicitly represent the invalid observation and exclude it from
acceptance; do not merely log it and resume as a clean run. Avoid destructive restoration
or rollback that loses another coroutine's successfully committed changes.

Review `_persist_interrupted`: it logs a failed write then returns, explicitly admitting
the position will be missing after restart. Best-effort logging alone is insufficient
for a reconciled trial. Distinguish graceful cancellation from abrupt process death;
catching an exception after leg 1 is not a durable checkpoint before leg 2.

Preserve cancellation semantics: `PaperExecutor.execute` wraps `CancelledError` in
`ExecutionInterrupted`, while `Scanner.paper_trade` rethrows the wrapper rather than
its stored cause. Test that operator shutdown does not become a misleading execution
failure after partial exposure has been handled.

Acceptance: cash, holds, trades and exposure agree between memory and restart, or the
run is explicitly failed/incomplete with actionable recovery. Settlement credits
persist once, including retry and immediate-restart failure paths.

### 2. Repair the interruption tests and pass all quality gates

The two failures are in `tests/integration/test_accounting_durability.py`:

- `test_an_interruption_between_the_legs_keeps_the_exposure_it_created`
- `test_an_interrupted_attempt_is_not_counted_as_hedge_evidence`

Both monkeypatch `scanner._book_for_leg` with `raising=False`. That method does not
exist in the execution path; `paper_trade` creates a local provider closure, so both
tests execute a normal fully hedged trade. Inject a failure through the real provider
boundary after a verified leg-1 fill. Assert interrupted status, persisted exposure,
fees, balance conservation, evidence exclusion, and restoration. Use guaranteed cleanup
even when assertions fail. Add cancellation during shutdown and failed partial-write
coverage; do not weaken the assertions to make the suite green.

Add an actual `_cmd_settle` integration test against an inverted persisted pair,
unknown pair refusal, and explicit differing venue outcomes. Preserve the passing
settlement retry regressions.

Clean the 14 lint findings (exception naming, async timeout API, long line, nested
contexts and unused suppressions), four formatting failures, and three test typing
errors (redundant narrowed enum comparison and incorrectly typed TestClient app access).
Run every verification command in the table independently and record the final counts.

### 3. Produce the reproducible trial configuration and runbook

Deliver a dedicated example configuration and exact commands for a dedicated trial
SQLite database at head; preserve the existing database. Pin:

- Paper-only posture; Kalshi production public REST and Polymarket **US** REST.
- One explicit watched pair, independently reviewed and approved; configuration
  cannot grant approval. Keep placeholder IDs visibly unusable until actual IDs exist.
- Starting USD balance per venue, finite trade/open-exposure limits, minimum cash,
  polling cadence, measured freshness budget and request budget including leg refetches.
- Initial 30–60-minute duration, watchdog thresholds, stop/restart procedure and exit
  codes, settlement evidence/commands, reporting and reconciliation procedure.
- Exact reviewed revision, configuration identity and evidence-directory/run identity.

Update `docs/claude-paper-trial-handoff.md`, `docs/trial-readiness.md`, and
`docs/operations.md` so partial work and old test counts are not presented as current
completion. Public REST does not require building the deferred authenticated WebSocket
path. Keep real-order execution structurally disabled.

### 4. Demonstrate unattended behavior offline

Run an accelerated fixture/mocked-transport trial with fresh temporary storage. Inject
feed failures, scan failures, persistent storage failure, capital depletion, interruption
between legs, restart, and settlement/restart. Export a reconciliation report showing
opening cash + durable cash entries = ending cash per account, reservations, open
positions, rejected attempts, evidence grades and stop causes. Include an abrupt-stop
scenario or explicitly document the remaining crash-recovery limitation. An aggregate
unit-test count is not a substitute for this runtime exercise.

### 5. Prepare the supervised live-data launch package

After steps 1–4 pass, identify the exact market IDs, both complete rulebooks, fee
assumptions, freshness/request budget, applicable operator confirmations and approval
record. Follow the bounded smoke workflow or its documented named-market alternative.
If there is no equivalent pair, report that as the blocker; do not widen discovery or
weaken matching. Validate current venue behavior at that time rather than treating old
smoke evidence as current.

The present assignment is review and next-step preparation. Do not interpret this
handoff as authorization to contact live venues, approve a pair for the operator,
start a live-data run, or place real orders. Prepare a concrete launch for the operator.
The previous Claude task also explicitly restricted development to offline fixtures.

After the supervised 30–60-minute run is authorized and reviewed, extend to a proposed
24–72-hour observation. Report no-trade runs honestly: they validate operations but
not fills, hedge quality or settlement. Separate measured, unchanged-reread and modelled
evidence. Actual resolution/settlement remains pending if it did not occur.

## Ready-to-paste continuation prompt

Work in `/Users/owenlin/Documents/PP/ab_betting`. Read
`docs/claude-next-steps-2026-09-10.md` and the existing handoff before editing. Complete
steps 1–4 in order, preserving the uncommitted work. Prioritize the reproduced loss of
paper trades/cash debits across failed persistence and restart, then real interruption
tests and cancellation semantics. Make every quality gate pass, deliver the dedicated
trial configuration/runbook, and run the offline fault-injection acceptance exercise.
Update the handoff with actual evidence. Prepare step 5 as a concrete supervised launch
package; use no live venue requests and do not start a live-data trial under this prompt.
Keep real orders disabled, Polymarket US, explicit watched markets and human approval.
Do not expand into WebSockets, Global, additional venues or real-order features.
