# Project completion assessment — 2026-09-08

Finish line confirmed by the operator: **a reliable paper-trading trial** on Kalshi and Polymarket US, using public REST data, explicit watched markets, reviewed pairs, durable simulated accounts, and usable trial evidence.

**Assessment: the core system exists, but the current working tree is not ready for a reliable unattended trial. Budget 48–88 hands-on hours, plus 24–72 hours of observation and any external waiting.** This is a planning estimate, not an exact prediction. With one experienced engineer, operator availability, and an accessible equivalent pair, allow approximately 2–3 calendar weeks. Market resolution, venue/account confirmations, or lack of suitable pairs can extend that substantially.

Do not use “six phases implemented” or a passing-test percentage as a completion percentage. Much of the remaining work is demonstrating behavior across failures and on real data. No trustworthy exact percentage follows from the repository.

## Evidence and limits

- Assessed current source, the existing graph, plans, operational documentation, migrations, integration tests, and the local SQLite database in read-only mode.
- Fresh offline suite: **988 passed; 5 live tests deselected**. Ruff lint passed; all 171 files passed formatting. Application mypy passed for 87 files; test mypy passed for 58 files. `event-arb doctor` verified that real-order operations remain disabled. One third-party deprecation warning remains.
- Fault injection used fixture adapters and temporary SQLite databases. The startup reproduction mocked discovery; it transmitted no request. No live trial, external approval, settlement of project data, or order was performed.
- There are **29 modified/untracked paths** before this report, including the new accounting modules, migration, and regression tests. Their presence counts toward implemented work, but the release still needs a reviewed, reproducible revision. The committed history alone understates progress.
- `data/event_arb.db` is at revision `3ea85fd4182a`; the current application expects `6b2f1c9a7d40`. It contains zero markets, pairs, opportunities, and paper trades. This is evidence about this database, not proof that no trial exists elsewhere.
- Current external API behavior, account eligibility, venue permissions, and fees were not independently verified. This is a repository delivery assessment, not a current legal or trading recommendation.

Graph vocabulary used: paper, trial, readiness, settlement, portfolio, smoke, approval, freshness, watchdog, restore. The graph supplied architectural pointers; direct source inspection and reproductions supplied the new findings below.

## What is already implemented

The project has fixture and read-only REST adapters; normalized order books; candidate matching and human approval; approval invalidation on rule changes; depth-aware opportunity calculations; fees and risk buffers; a dashboard; automated paper execution; and persistence and retention infrastructure.

The September 7 accounting backlog has largely been implemented in the current working tree: distinct venue accounts, fee-inclusive reservations, capital and exposure limits, a durable ledger, trade restoration, settlement credits, separate execution and settlement reporting, shared preflight, and a watchdog. Existing regressions cover US USD/USD and Global USD/USDC accounts, sequential capital depletion, interrupted reservations, and successful settlement/restart lifecycles. These are not entire features that need rebuilding.

## Remaining work and estimates

Each range includes focused regression tests and review. Tasks are work packages, not claims that every undiscovered defect has been enumerated.

| Order | Work package | Hands-on hours | Acceptance evidence |
| --- | --- | ---: | --- |
| 1 | Repair failure accounting, watchdog termination, and CLI completion status | 8–14 | Repeated scan/trade failures pause and stop despite successful refreshes; failed audit storage cannot prevent stopping; a stopped/failed runner wakes the CLI and returns a failure result. |
| 2 | Complete accounting failure recovery and settlement correctness | 12–22 | Failed writes, retries, cancellations between legs, and restart preserve or explicitly invalidate simulated exposure and cash; settlement payouts persist exactly once; CLI inversion handling is verified. |
| 3 | Move static startup checks before discovery and storage-dependent reads | 4–8 | CLI and enabled dashboard trial refuse invalid scope before adapter requests; old schemas produce an actionable refusal before incompatible queries. |
| 4 | Prepare a reproducible trial configuration and release/runbook | 4–6 | Dedicated database at current schema; fixed paper balances, limits, REST cadence, pair scope, run duration, stop rules, settlement procedure, and reviewed revision recorded. Update stale status documents. |
| 5 | Validate bounded live data and establish an approved pair | 4–8 | Smoke evidence covers actual books, identifiers, normalization, fees and freshness; operator reviews both complete rulebooks and records the required confirmations. |
| 6 | Run and inspect a supervised trial | 4–6 | Initially 30–60 minutes; request scope, books, rejections, fills, balances, stop/restart, and evidence grades reconcile. Extend only after reviewing results. |
| 7 | Run a longer observation and produce a reconciled acceptance report | 4–8 | Proposed 24–72-hour run; no unexplained cash/exposure drift; storage/feed failures handled visibly; replay and settlement results reconcile. Report observation limits. |
| | **Known work subtotal** | **40–72** | |
| | **Planning allowance for integration/live-schema surprises** | **8–16** | |
| | **Budget to reserve** | **48–88** | |

The 24–72 hours in item 7 are elapsed runtime, not full-time labor. Engineering work and operator review may overlap. External onboarding or approvals are not assigned a promised turnaround. The estimate assumes one pair and SQLite; additional pairs, PostgreSQL deployment certification, or broader discovery increase scope.

## Specific engineering findings

### 1. Repeated scan/trade failures can evade the watchdog — reproduced

`src/event_arb/runtime.py:233` resets consecutive failures after refresh, before scanning and paper execution. Thus one failed scan per successful refresh never accumulates beyond one failure. An offline reproduction of 15 success-refresh/failure-scan transitions ended with `consecutive_failures=1`, no pause, and no stop. Paper execution errors feed the same counter.

Track failure categories separately or reset only after the relevant operation succeeds. Test the complete runtime cycle, not only calls to the failure counter.

### 2. Storage failure can prevent the stop action — reproduced

`src/event_arb/runtime.py:384` awaits a database audit write before setting the stop event. With the failure count at the stop threshold and audit storage unavailable, `_note_failure` raised and the stop event remained unset. The pause path also awaits the same storage. A failure handler cannot depend on the failed subsystem to complete its essential action.

Separately, `src/event_arb/main.py:343` waits for duration or an indefinitely pending event, rather than runner termination, and returns zero after the summary. Connect watchdog/task failure to CLI termination and a meaningful exit status. This latter finding is source-inspected, not a timed CLI reproduction.

### 3. Settlement retry can permanently omit a payout — reproduced

`src/event_arb/scanner.py:761` applies settlement to the in-memory trade and ledger before its database transaction succeeds. In a fixture test, a failed trade-row write left the in-memory trade settled and credited $100. Retrying in the same process returned `already_settled=True` with zero new ledger entries. After restart the trade was settled, but the $100 payout was absent from its account.

Make persistence and in-memory publication consistent, or rebuild the durable mutation on retry. Apply the same failure review to execution and reservation writes: `paper_trade` publishes portfolio changes before persistence, and reservation persistence precedes the funded path's cleanup block.

### 4. Interrupted legs and CLI settlement need completion checks — source-inspected

`src/event_arb/paper/executor.py:401` awaits leg 2 after simulating leg 1; the trade is only persisted after `execute` returns. An interruption there has no durable leg-1 checkpoint. Releasing the abandoned reservation alone does not document the simulated fill or prove recovery. Decide and test whether interrupted attempts retain simulated exposure or are explicitly excluded as incomplete observations; do not silently count them as clean runs.

`src/event_arb/main.py:665` restores the portfolio for `settle`, but does not load pairs. Later inversion inference looks up `scanner.state.pairs`, which is empty on that CLI path. Require explicit per-venue outcomes, or recover trustworthy inversion metadata, and test the actual command against an inverted persisted pair. Passing `--polymarket-outcome` explicitly avoids the inference path while it is repaired.

### 5. Preflight occurs too late to prevent out-of-scope discovery — reproduced

`src/event_arb/main.py:302` calls discovery before checking for watched pairs. With live settings and no watched pairs, a mocked discovery was awaited once before the command refused. `Scanner.discover` uses catalogue discovery when scope is absent. The dashboard startup has the same ordering problem (`src/event_arb/api/app.py:103`) and ignores the result of its schema check before restore.

Separate static configuration/schema gates from measured freshness checks. Run the static checks before network access; measure only explicitly permitted market scope afterward.

## External critical path

The latest documented smoke run, August 26, stopped after two catalogue requests and zero candidates; no books were fetched. Offline scoring fixes do not prove the current catalogues contain equivalent contracts. The bounded smoke plan allows at most 31 requests and still needs a separately authorized run. See `smoke-test-plan.md` and `zero-candidate-diagnosis.md`.

If the bounded catalogue sample still yields no useful pairs, explicitly named market IDs are the documented narrower alternative. Do not assume pagination, relaxed matching, or additional venues are prerequisites. A watched pair is not an approved pair.

The operator confirmations recorded in `trial-readiness.md` remain evidence gaps. Record their status for the intended read-only REST trial; do not treat the document's separate authentication/WebSocket tasks as prerequisites for this finish line.

**Critical path:** reliability repairs → reviewed revision and trial database → scoped book validation and human pair approval → supervised trial → longer run and acceptance report. Operator confirmations and pair research can proceed alongside engineering.

## Definition of done

1. All quality gates and the new fault-injection regressions pass on the release revision.
2. Both startup paths enforce explicit live scope before requests and use the expected database schema.
3. At least one actual pair is reviewed and approved; required operator confirmations and fee assumptions are recorded.
4. The supervised and longer trials stay inside declared scope and collect usable, fresh data. Zero opportunities is reported honestly and does not establish fill quality.
5. Cash, reservations, open exposure, trade records, and settlements reconcile across retries and restarts. Mid-leg interruption has a tested, visible accounting policy.
6. Feed/storage failures pause or stop reliably, with a visible reason and correct process status.
7. The acceptance report distinguishes observed, unchanged-reread, and modelled evidence. If no eligible simulated trade or actual settlement occurs, those validation gates remain pending; waiting for market resolution is not a coding estimate.

A 24–72-hour run is a proposed operational acceptance window, not proof of profitability, calibrated hedge success, or readiness for real-money execution. Those require different evidence and a broader scope.

## Deferred from this finish line

Real-order adapters and Phase 7; Polymarket US signing/WebSocket phases U5/U6; venue-wide pagination; sportsbook licensing/integration; and generic multi-venue leg migration. None is necessary to establish a reliable, explicitly scoped, REST-based paper trial.

Deliver the seven work packages above before expanding the venue or feature roadmap. Keep `trial-readiness.md`, `operations.md`, and the September 7 review consistent with the final behavior: their current completion and authentication descriptions disagree with the working tree.
