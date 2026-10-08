# Paper-trial engineering handoff

> **Current as of 2026-09-22, after the second review.** The September 10 review
> ([next steps](claude-next-steps-2026-09-10.md)) found 1011 passed / 2 failed, 14 lint
> findings, 4 formatting failures, 3 test typing errors, and a reproduced accounting gap
> where a failed write silently erased simulated fills across a restart. All of that was
> addressed, and the tree then passed 1042 tests and every gate.
>
> The September 22 review ([completion prompt](claude-paper-trading-finish-2026-09-22.md))
> reproduced three further gaps in that same passing tree. None was a missing feature; each
> was a check returning the right-looking answer for the wrong reason — a dead feed
> reported as a quiet market, a restart upgrading incomplete evidence to complete, and a
> reconciliation that re-added the ledger to itself. All three are fixed, with regression
> tests that fail against the old code. See [package 7](#package-7--the-september-22-gaps-done).
>
> The verification table below is a fresh run, not a remembered one. What has **not**
> happened: no live-data trial has been run, and no live venue request has been made from
> this work.

Working file for the reliability work described in
`docs/project-completion-assessment-2026-09-08.md`. Updated as packages complete.

- **Branch:** `smoke-test-preflight-blocked`
- **Base commit:** `c4f0cf0` ("Keep §7's structural safety claims running instead of remembered")
- **Everything below is uncommitted.** The working tree carries the September 7 accounting
  work plus this session's changes.
- **No live venue request has been made.** All work used fixtures, mocked transports and
  temporary SQLite databases.

## Status at last update

| Package | State |
| --- | --- |
| 1. Watchdog, shutdown, CLI exit status | **implemented and verified** |
| 2. Durable accounting and settlement | **implemented and verified** |
| 3. Startup gates | **implemented and verified** |
| 4. Trial configuration and runbook | **delivered** — [`config/trial.env.example`](../config/trial.env.example), [runbook](paper-trial-runbook.md) |
| 5. Offline unattended-behaviour exercise | **delivered** — `tests/acceptance/` |
| 6. Supervised live-data trial | **prepared, not started** — [launch package](live-trial-launch-package.md); needs operator authorization |
| 7. The September 22 gaps | **implemented and verified** — feed coverage, the run journal, independent trade/ledger integrity |
| 8. US acceptance + executable launch procedure | **delivered** — `tests/acceptance/test_us_trial_acceptance.py`, `scripts/run-paper-trial.sh`, `event-arb manifest` |

Test count over the sequence: **988** at the start of the September 7 session, **1005**
after packages 1 and 3, **1013** at the September 10 review (1011 passing, 2 failing),
**1042** at the September 22 review, **1130** now.

## Package 1 — watchdog and shutdown (done)

Reproduced both assessment findings against the tree first, then fixed them.

- *Finding 1 reproduced:* 15 consecutive failed scans behind successful refreshes ended
  with `consecutive_failures=1`, no pause, no stop.
- *Finding 2 reproduced:* with audit storage dead at the stop threshold, `_note_failure`
  raised and the stop event stayed unset while `stopped_reason` was already set.

Changes, all in `src/event_arb/runtime.py` unless noted:

- Failure streaks are now **per loop** (`TrialStats.consecutive_failures_by_loop`).
  `_note_success(what)` clears only that loop's streak and only that loop's pause.
  `consecutive_failures` is the worst streak, so existing readers still work.
- `_stop_trading` sets `stopped_reason` and the stop event **before** any I/O.
  `_audit_decision` writes the audit row best-effort and can never block the decision.
- Execution failures escalate straight to a stop (`PAUSING_LOOPS = {"refresh", "scan"}`).
  Pausing execution because execution failed suppresses the only thing that could advance
  its streak, so it froze one below the stop threshold and paused forever.
- A refused trade clears the streak — running out of simulated money is a result, not a fault.
- `_task_finished` done-callback: a cooperating task that dies wakes the whole trial and
  sets `task_failed`.
- `wait_until_stopped()` and the `stopped` / `failed` properties. The bound is the
  caller's, via `asyncio.timeout`, so a run with a duration and one without take the same
  path — and "it stopped itself" stays distinguishable from "the clock ran out".
- `src/event_arb/main.py`: `_cmd_trial` waits on the runner rather than on the clock or on
  an `asyncio.Event()` nothing ever set, and returns **1** on a watchdog stop or dead task.
  An operator Ctrl-C still exits 0.

Tests: `tests/integration/test_trial_failure_accounting.py` (10 tests) drives complete
runtime cycles, not the counters in isolation — which is why the old suite passed.

## Package 3 — startup gates (done)

- *Finding 5 reproduced:* live settings with no watched pairs awaited `Scanner.discover`
  **once** before refusing. Now `discover.await_count == 0`.

Changes:

- `src/event_arb/trial_preflight.py`: split into `run_static_preflight(settings, database)`
  — safety posture, ingestion mode, watched scope, schema; no network, no scanner — and
  `run_preflight(..., static=...)`, which carries the static findings forward so the
  operator still sees one combined report. `PreflightStage` records which half a report is.
- `src/event_arb/main.py` and `src/event_arb/api/app.py`: both run the static gate **before**
  `discover()`. The dashboard previously ignored its own `ensure_schema` result and
  generated pairs from a venue-wide catalogue; it now refuses and serves read-only.
- **New defect found and fixed** (not in the assessment):
  `src/event_arb/persistence/schema.py` used a single sentinel table (`paper_trades`) to
  decide whether a database was empty. A database predating that migration read as empty,
  so `ensure_schema` created the missing tables beside the stale ones and stamped the result
  at head — reporting a current schema whose older tables still had the previous shape, which
  then failed on the first read of a migrated column. Emptiness is now "no declared table
  present at all".

Tests: `tests/integration/test_startup_gates.py` (7 tests) drive `_cmd_trial` and
`create_app` with discovery mocked to count awaits.

## Package 2 — durable accounting and settlement (done)

*Blocker reproduced first.* With `PaperTradeRepository.record` patched to raise during a
fixture-funded trade, memory held one trade and balances of `kalshi:USD=9951.2500` /
`polymarket_us:USD=9960.5900`. Closing and reopening the same temporary database showed
zero trades and `10000.0000` in both accounts, while the restore report still said
`is_complete=True`. Both figures match the review's independent reproduction exactly.

The rule now enforced, and the reason it is the rule, are written up in
`src/event_arb/paper/durability.py`: **nothing is published to memory before storage has
accepted it.** A paper portfolio is only meaningful because it can be reconstructed; a
balance that exists solely in a live process is a number that disappears at the next
restart while the report still calls the run clean.

Changes:

- `PaperPortfolio.plan_record` / `commit_record` split planning from applying, so a caller
  can store the entries and only then publish them. `plan_record` returns the **full**
  deterministic entry set, including entries already applied — returning only the newly
  applied subset is precisely what made a failed settlement write permanent.
- `Scanner.paper_trade` stores the trade row and its ledger entries in one transaction,
  retried once (both writes are keyed by their own ids, so a retry converges rather than
  duplicating, and nothing is re-executed), and publishes only after the commit. A write
  that will not land raises `TradeNotDurableError`, and the attempt is recorded as an
  `InvalidatedAttempt`: counted, excluded from every rate, and reported.
- `PaperExecutor` gained a **leg-1 checkpoint** called before leg 2 is attempted.
  Recording the exposure in an `except` clause only runs when the process survives long
  enough to catch, so an abrupt death between the legs left nothing behind. The checkpoint
  publishes a *snapshot* of the trade, not the live object the executor goes on mutating.
- Cancellation semantics preserved: `paper_trade` re-raises the stored `CancelledError`
  rather than the `ExecutionInterruptedError` wrapper, once the exposure has been handled.
  An operator shutdown is not an execution failure.
- `Database.session` rolls back on `BaseException`, not `Exception`. A cancellation
  arriving mid-transaction left the session un-rolled-back, and an aiosqlite connection
  returned to the pool with an open transaction is invalid — which surfaced later as a
  query finding an empty database rather than as an error at the point of failure.
- A failed **settlement** write is recorded as a divergence rather than only raised.
  Settlement is the one place memory legitimately leads storage — the credit is idempotent
  and a repeat converges — but until that repeat lands the run is quoting cash a restart
  would not find, so the daily report says so and a successful retry clears it.
- New: `event-arb reconcile`, `event-arb trial --reconciliation-json`, and
  `src/event_arb/paper/reconciliation.py`, which re-derives every account from its ledger
  entries and checks `opening + movements == ending` **per account** — never in aggregate,
  because a USD gain and a USDC loss net to something meaningless.

Tests: `tests/integration/test_accounting_durability.py` (16), which now injects through
the **real** provider boundary. The two tests the review found broken patched
`scanner._book_for_leg` with `raising=False`; no such method exists in the execution path,
so both executed an ordinary fully hedged trade while asserting nothing. They now fail the
leg-2 book read after a verified leg-1 fill. Also
`tests/integration/test_settle_command.py` (10), which drives the real argparse namespace
through `_cmd_settle` — inverted pair, aligned pair, unknown-pair refusal, explicit
per-venue outcomes, dry run, attribution, listing, and a repeat that credits once.

## Package 4 — trial configuration and runbook (done)

- `config/trial.env.example`: paper-only posture, Kalshi production public REST +
  Polymarket **US** REST, one explicitly watched pair with deliberately unusable
  placeholder ids, per-account starting balances, finite trade/open/unhedged limits, a
  minimum cash floor, cadence, freshness bound and watchdog thresholds. Verified to load:
  `doctor` reports a 0.80 s estimated refresh batch, a required book age of 1300 ms
  against the configured 2000 ms, and `freshness budget : OK`.
- `docs/paper-trial-runbook.md`: run identity and evidence directory, dedicated database
  at head (`alembic upgrade head` — never `./data/event_arb.db`, which is two migrations
  behind at `3ea85fd4182a`), the four commands that reach a venue marked as such, exit
  codes, request budget including leg re-fetches, stop/restart, settlement, reconciliation,
  how to report a no-trade run honestly, and the exit criteria for a 24–72 hour extension.

## Package 5 — offline unattended-behaviour exercise (done)

`tests/acceptance/test_offline_trial_acceptance.py` assembles a real `TrialRunner` over
fixture data with fresh temporary storage, injects one failure per scenario, and reconciles
every account from storage afterwards: dead feed, failing scan, storage refusing a trade
write, capital depletion, interruption between the legs, abrupt stop with the interruption
handler disabled, settlement failure repaired across a restart, and an uninjected control.
Each writes a reconciliation artifact:

```bash
EVENT_ARB_ACCEPTANCE_OUT=evidence/offline-acceptance \
    .venv/bin/python -m pytest tests/acceptance -p no:cacheprovider
```

A **US** exercise runs alongside it — `tests/acceptance/test_us_trial_acceptance.py`,
Kalshi + Polymarket US with USD on both legs, artifacts prefixed `us-`. 19 scenarios in
total between the two files.

A real `SIGKILL` on a real subprocess is now tested in
`tests/integration/test_abrupt_termination.py`, so the Global exercise's simulated process
boundary is no longer the only coverage of that path. What remains genuinely untested is
SQLite's durability under a power loss or on a filesystem that lies about `fsync`; the kill
test exercises one host's behaviour and does not establish the general case.

## Verification commands

```bash
.venv/bin/python -m pytest -p no:cacheprovider     # live tests deselected by default
.venv/bin/ruff check src tests
.venv/bin/ruff format --check src tests
.venv/bin/mypy src
.venv/bin/mypy --config-file mypy-tests.ini tests
.venv/bin/event-arb doctor
```

Result on 2026-09-22 after the second review's work, every command run independently:

| Check | Result |
| --- | --- |
| `pytest -p no:cacheprovider` | **1130 passed**, 5 live tests deselected, 31.6 s |
| `ruff check .` | **All checks passed** |
| `ruff format --check .` | **201 files already formatted** |
| `mypy` | **Success**, 93 source files |
| `mypy --config-file mypy-tests.ini` | **Success**, 76 source files |
| acceptance exercise with `EVENT_ARB_ACCEPTANCE_OUT` | **19 passed**, 31 artifacts retained |
| `event-arb manifest` | 211 files hashed by content, 35 untracked, `config/trial.env` excluded |

The September 10 run of the same table read 1042 passed / 87 src files / 66 test files.
The +88 tests are the regression coverage for this session's three gaps plus the US
acceptance exercise, the request budget, the launch script and the manifest.

One third-party Starlette deprecation warning remains (`httpx` with `starlette.testclient`).

## Remaining work

Everything left needs live data or an operator's judgement, and none of it is a code gap:

1. **Identify the actual pair.** Two real market ids, both complete rulebooks read by a
   human, fee assumptions recorded, and the `outcomes_inverted` determination made. If no
   equivalent pair exists, that is the finding — do not widen discovery or weaken matching
   to produce one.
2. **Re-validate venue behaviour at the time of the run.** The August 26 smoke-test
   evidence is not current; treat it as history.
3. **Run the supervised 30–60 minute trial** per the runbook, with the operator's explicit
   authorization. It is the first live-data run this project will have made.
4. **Review it, then propose** the 24–72 hour observation as a separate authorization.


## Package 7 — the September 22 gaps (done)

Each was reproduced through the **real** boundary first, then fixed, then pinned.

### 7.1 Adapter failure bypassed the watchdog and the preflight

`Scanner.refresh_books` catches each `fetch_orderbook` error, marks the book stale and
continues, returning normally even when every request failed. `_check_freshness` judged
*duration*, and a batch in which everything raises returns fast.

*Reproduced:* both adapters' `fetch_orderbook` replaced with a raising function, full
preflight plus an accelerated runner at 2/3 thresholds — **preflight passed, 62 refreshes
counted, 0 errors, no stop reason, `runner.failed=False`**. After the fix, the same script
gives `PREFLIGHT passed: False, blocking: ['feed_coverage']` and
`stopped_reason: 3 consecutive failures (refresh)`, `runner.failed=True`.

- New `src/event_arb/orderbooks/coverage.py`: `RefreshOutcome` carries `required`, `books`,
  `failed`, `unusable`, `skipped`, `requests`, and `missing`/`is_complete`.
- `refresh_books` returns it. An empty book — no asks on either outcome — is stored and
  marked stale but **not** counted as coverage; the previous, better-looking snapshot must
  not survive to be priced as current.
- `_check_feed_coverage` in the preflight blocks a trading run on missing coverage or on a
  freshly fetched book that is already stale. Observation-only warns instead: watching a
  feed fail is legitimate, as long as the report says that is what happened.
- `_cycle_loop` treats an incomplete batch as a refresh failure and skips the scan.
- `_health_loop` now escalates sustained degradation to a stop, which its docstring had
  promised and its code never did, and bounds a health check that itself keeps raising.
- `TrialStats` gained `refreshes_incomplete` and `feed_was_healthy`.

Covered by `tests/integration/test_feed_coverage.py` (16 tests): one-venue and both-venue
outages, recovery, empty books, a healthy no-opportunity run that must **still pass**, and
both health-loop escalation paths.

### 7.2 Invalidation vanished across a restart

*Reproduced:* force `PaperTradeRepository.record` to fail through every checkpoint attempt.
Before close, `invalidated=1, complete_evidence=False`; **reopening the same database gave
`invalidated=0, complete_evidence=True`**.

Writing it to the database cannot fix this: the attempt is invalidated *because* the
database refused it. New `src/event_arb/paper/journal.py` — an append-only, `fsync`'d
sidecar beside the database, carrying:

- `attempt_invalidated`, replayed into the portfolio on restore;
- `run_started` / `run_finished`, so an **unfinished run** is visible. A killed process
  cannot record its own death, but it can fail to record its survival;
- `attempt_resolved`, so a repaired write stops counting **without** erasing the failure;
- `run_acknowledged`, appended by `event-arb acknowledge-run <id> --by … --note …`.

`DailyReport` gained `unfinished_runs`, and `is_complete_evidence` fails closed on either.
Covered by `tests/integration/test_run_journal.py` (9 tests) and
`tests/integration/test_abrupt_termination.py` (2 tests, real `SIGKILL` on a subprocess).

### 7.3 Reconciliation re-added the ledger to itself

`opening + movements == ending` has both sides computed from the same ledger.

*Reproduced:* one fixture trade, its two `trade_debit` entries deleted in disposable
storage — **one trade still retained, `reconciles=True`, `evidence_is_complete=True`**, and
cash risen by 47.00 on Kalshi and 38.00 on Polymarket US.

New `src/event_arb/paper/integrity.py` re-derives each retained trade's implied entries
from the **trade rows** (stored separately from the ledger) via `plan_record`, and reports
`missing_entry`, `wrong_amount`, `wrong_account`, `wrong_kind`, `unexpected_entry` and
`orphan_entry`. Retention compaction is respected through a boundary read from the
carry-forward entries' own `created_at`, so a compacted trade is reported as *not verified*
rather than as corrupt — a check that flagged all compacted history would train an operator
to ignore it.

`ReconciliationReport.reconciles` and `.evidence_is_complete` both consult it, and
`.unfinished_runs` is reported alongside.

**Stated limitation.** This compares two independently stored things. It cannot catch a
rule that is wrong but applied consistently: if `plan_record` computed the wrong fee, the
write and the check would agree on it. The fixture accounting tests cover that class.

`_cmd_reconcile` also documented itself as read-only while calling `restore_portfolio`,
which reaps stale holds **and persists the release** — so inspection mutated the database,
and inspecting a database a trial was using released that trial's live reservations.
`Scanner.inspect_portfolio` / `paper.restore.inspect_portfolio` are the non-mutating path.

Covered by `tests/integration/test_reconciliation_integrity.py` (19 tests), including a
test pinning that the per-account arithmetic *alone* still closes on the corrupted database
— if that ever starts failing, the two checks have stopped covering different questions.

## Package 8 — proving the target, and an executable launch (done)

- `tests/acceptance/test_us_trial_acceptance.py`: Kalshi + Polymarket **US**, USD on both
  legs, through the assembled runtime — fresh migration, approval persistence across a
  restart, preflight, healthy run, funded execution against two distinct USD accounts,
  interruption and restart, settlement **aligned and inverted**, adapter outage, unfinished
  run. Global is kept as legacy coverage.
- `.gitignore` names `config/trial.env` exactly; `.env.*` never matched it, because
  gitignore matches that pattern against the basename `trial.env`.
- The pair review page renders an authenticated approval command with `curl -fsS`; without
  `-f`, curl exits **0** on a 401, so a refused approval looked accepted.
- `EVENT_ARB_REVIEW_ONLY=1` serves a dashboard that cannot start a trial. The example
  config enables the trial, so `serve` against an approved database was starting one as a
  side effect of looking at it.
- `scripts/run-paper-trial.sh` reads `PIPESTATUS[0]`, not `$?`, which after a pipeline is
  *tee's* status — a trial that exited 1 recorded `exit: 0` in its own evidence.
- `event-arb manifest` hashes source **content**, including untracked files, excluding
  secret-shaped paths by name and rendering `SecretStr` as its type.
- The request budget is measured, not asserted: **one** request per venue per cycle for one
  pair, ≤ 720 per venue per hour at 5 s — not 1,440 — plus two per simulated trade.
