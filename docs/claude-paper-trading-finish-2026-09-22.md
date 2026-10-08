# Paper-trading readiness review and Claude completion prompt

Reviewed September 22, 2026, against the working tree in
`/Users/owenlin/Documents/PP/ab_betting`, branch `smoke-test-preflight-blocked`,
base commit `c4f0cf0`. Substantial implementation remains uncommitted; preserve it.
The existing graph was used for navigation, then findings were checked against source.
No production venue was contacted, no pair was approved, and no existing database was
modified. Failure reproductions used fixtures and disposable SQLite databases.

## Verdict

The fixture simulator is usable now. The live-data paper trial is close, but should not
be declared ready yet. Most earlier engineering work is complete: durable leg-1
checkpoints, execution interruption handling, per-account capital limits, settlement,
restoration, startup gates, runtime supervision and an operating runbook exist.
Do not rebuild those features or repeat the September 10 task list as if it were current.

Fresh verification:

| Check | September 22 result |
| --- | --- |
| `.venv/bin/pytest` | 1042 passed, 5 live tests deselected; 34.17 seconds |
| `.venv/bin/ruff check .` | Pass |
| `.venv/bin/ruff format --check .` | Pass; 186 files |
| `.venv/bin/mypy` | Pass; 89 source files |
| `.venv/bin/mypy --config-file mypy-tests.ini` | Pass; 66 source files |

One third-party Starlette/httpx deprecation warning remains. It is not the launch blocker.
The existing `data/event_arb.db`, inspected read-only, is still at `3ea85fd4182a`;
current application head is `6b2f1c9a7d40`. There is no dedicated trial database in `data/`.
The example trial configuration still contains placeholder market IDs.

## Confirmed gaps

### 1. Adapter-level feed failure bypasses the watchdog and freshness preflight

`Scanner.refresh_books` catches individual `fetch_orderbook` errors, marks the book stale,
and continues (`src/event_arb/scanner.py:328`). It returns normally even if every request
failed. `_check_freshness` judges duration, not successful coverage of the required markets
(`src/event_arb/trial_preflight.py:435`). The runtime consequently counts a successful
refresh and resets its refresh failure streak (`src/event_arb/runtime.py:260`).

Offline reproduction: start with an approved fixture pair, replace both adapters'
`fetch_orderbook` methods with a raising function, run full preflight and an accelerated
runner with pause/stop thresholds 2/3. Preflight passed; 17 refreshes were counted with
zero errors, no stop reason and `runner.failed=False`.

This does not show stale books executing: stale-book rejection remains present. It shows
an unusable feed being reported as successful operation. Real adapter health can later
pause trading, but `_health_loop` only pauses/resumes; it never escalates persistent
unhealthy status to a stop despite its docstring. The existing acceptance dead-feed test
patches `Scanner.refresh_books` itself, bypassing the error-swallowing boundary.

### 2. Invalidation evidence disappears after restart

`Scanner._invalidate` writes to in-memory lists and a metric only
(`src/event_arb/scanner.py:741`). `restore_portfolio` restores ledger entries and trade rows,
not those invalidations (`src/event_arb/scanner.py:1138`).

Offline reproduction: force `PaperTradeRepository.record` to fail through all checkpoint
write attempts. Before close: one invalidation and `evidence_is_complete=False`. Reopen
that same database: zero invalidations, `evidence_is_complete=True`, `reconciles=True`.
The current storage-failure acceptance scenario explicitly reports invalidations only in
an in-process note; it does not require them to survive restart.

Balances now remain consistent; that earlier defect is fixed. What remains broken is the
claim that a fresh-process report knows whether the prior run lost an attempted execution.

### 3. Reconciliation does not independently validate trades against cash movements

`reconcile_accounts` groups ledger entries and compares them with `portfolio.balance`,
which sums the same ledger (`src/event_arb/paper/reconciliation.py:181`,
`src/event_arb/paper/ledger.py:177`). This arithmetic can close even when required entries
are missing. It does not verify each stored trade's fills/fees/payout against its ledger.

Offline corruption reproduction: execute one fixture trade, delete its two `trade_debit`
entries in disposable storage, restart and reconcile. One stored trade remains, but the
report still says `reconciles=True` and `evidence_is_complete=True`. Cash increases from
9943.2600 to 9998.2600 on Kalshi and from 9961.0576 to 9999.0576 on Global. This reproduction
uses the legacy Global fixture helper; it makes no live Global requests.

Related source finding: `_cmd_reconcile` says it is read-only but calls
`Scanner.restore_portfolio`, which releases and persists all outstanding holds. It needs
a genuinely non-mutating inspection path; a reporting command should not perform recovery,
especially while another process owns an active reservation.

### 4. Launch instructions need an operational pass

- The acceptance suite explicitly selects Polymarket Global/USDC. Keep that coverage, but
  also exercise the actual Kalshi + Polymarket US/USD target through the assembled runtime.
- `config/trial.env` is not ignored by the current `.gitignore`, even though the runbook
  instructs the operator to put an API auth token there. Add an exact ignore rule while
  retaining `config/trial.env.example`.
- Approval curl shown by `pair_review.html` omits the bearer token required when the
  recommended API auth token is enabled. Supply an authenticated, tested procedure.
- The example enables the trial, so restarting `serve` with a persisted approved pair
  can start automatic paper trading. Specify a review-only dashboard procedure that cannot
  start a second runner, and retain static scope/schema checks in that mode.
- `git status --porcelain | shasum -a 256` identifies filenames/statuses, not file contents;
  it cannot identify the uncommitted implementation used for evidence.
- `trial ... | tee ...` followed by `$?` normally records tee's status, hiding a failed
  trial. Use a tested shell script with reliable pipeline failure/exit-code capture.
- For one pair, routine refresh is one request per venue per cycle: approximately 720 per
  venue per hour at a five-second cadence before processing time, not 1440. Each trade adds
  one refetch per venue, two total. Add startup/metadata/other requests separately and verify
  the actual call counts; do not simply repeat the current budget.
- `event-arb pairs` also makes live requests when the live configuration is sourced. Label
  all such commands consistently. Clarify required REST-only operator inputs versus deferred
  authenticated WebSocket work; the readiness document still lists real-key authentication
  among human gates even though this trial is credential-free by design.

## Operator inputs still required

No code can invent an equivalent market pair or the operator's approval. The launch
package still needs real Kalshi/Polymarket US market IDs, both complete rules, outcome
alignment, reviewed settlement sources, current fee/API verification, applicable operator
confirmations, a dedicated database and authorization for the bounded live-data run.
This review does not independently establish current venue terms, availability or fees.
If no equivalent pair exists, report that blocker without weakening matching.

## Ready-to-paste prompt for Claude

Work in `/Users/owenlin/Documents/PP/ab_betting`. Finish the remaining engineering needed
for our first supervised, REST-only, live-data **paper** trial on Kalshi production and
Polymarket **US**. Read `docs/claude-paper-trading-finish-2026-09-22.md` first, followed by
`docs/claude-paper-trial-handoff.md`, `docs/paper-trial-runbook.md`,
`docs/live-trial-launch-package.md`, `config/trial.env.example`, and applicable repository
instructions. Use the current working tree, including its uncommitted changes. Preserve
existing work. Do not rebuild the app or assume the older September 10 defects remain.

The current baseline passes 1042 offline tests, lint, formatting and both strict type
checks. Nevertheless, this review reproduced three gaps. Reproduce each through the real
boundary, implement a focused fix, and add regression coverage:

1. **Feed health and preflight.** Inject failure at adapter `fetch_orderbook`, not just at
   `Scanner.refresh_books`. Make refresh outcomes describe successful/failed required
   market coverage. Full preflight must reject missing, stale or unusable books even when
   requests return quickly. Persistent total or partial required-feed failure must reach
   the runtime's pause/stop policy and nonzero CLI result. Make health degradation and
   health-check exceptions have a bounded response. Keep stale-book protection, bounded
   scope and fixed freshness thresholds. Test outage on one venue, both venues, recovery,
   empty/unusable book results and healthy no-opportunity runs. An outage must not be
   reported as a healthy no-trade trial.

2. **Durable run completeness.** Make failed/invalidated attempts and unfinished runs
   identifiable from a fresh process. Use a persisted run/attempt lifecycle or another
   explicit fail-closed recovery design. Plan for the primary database being unavailable:
   recording the failure cannot depend solely on successfully writing to that failed
   database. An already durable unfinished-run marker can support recovery; choose and
   document a proportionate solution. A restart must not silently turn incomplete evidence
   into complete evidence. Keep publication-after-commit, idempotent retries and leg-1
   checkpoints. Exercise checkpoint/final-write/commit failure, restart immediately after
   failure, cancellation, and an abrupt process termination. Recovery decisions must be
   explicit and auditable; never fabricate a fill or erase a failed run's history.

3. **Independent, non-mutating reconciliation.** Validate retained trade fills, fees,
   account/product identities, settlement payouts and hold/release entries against their
   expected durable ledger entries. Detect missing, wrong-account, wrong-amount, unexpected
   or duplicated economic movements. Respect legitimate retention compaction with explicit
   verifiable boundaries rather than flagging all compacted history as corruption. Make
   incomplete restores/run records fail both CLI and JSON evidence gates. Separate report
   inspection from stale-hold recovery; `reconcile` must not alter the database or release
   another running process's holds. Test that inspection leaves storage unchanged. Include
   the reproduced missing-trade-debit case, missing fees, bad payout, interrupted exposure,
   settlement retry, retention, and active reservation cases.

4. **Prove the deployment target offline.** Extend acceptance to Kalshi + Polymarket US
   with USD on both legs, using existing US fixtures or mocked production-shaped REST
   responses. Cover fresh schema migration, pair review/approval persistence, startup,
   refresh/refetch, funded execution, interruption, restart, settlement and reconciliation.
   Include aligned and inverted outcomes. Keep Global coverage as legacy coverage, not as
   the sole proof of the US trial. Retain machine-readable acceptance artifacts. Add a real
   subprocess crash/restart test if practical; otherwise state precisely what remains
   untested and do not claim it was verified.

5. **Make the launch procedure executable.** Fix the operational findings in this review:
   ignore the local secret-bearing trial config, provide authenticated approval instructions,
   a review-only dashboard that cannot auto-start a trial on restart, reliable exit-code
   capture through tee, a content-based revision/config manifest including untracked source
   files without exposing secrets, and correct per-venue request accounting. Verify the
   fresh-database and review-to-trial commands against offline/mocked transports. Preserve
   `data/event_arb.db`; use a dedicated trial database. Update schema references if a new
   migration is needed. Keep `.env.example`/trial examples free of secrets and real approval.

6. **Verify and deliver.** Run independently:
   `.venv/bin/pytest`, `.venv/bin/ruff check .`, `.venv/bin/ruff format --check .`,
   `.venv/bin/mypy`, `.venv/bin/mypy --config-file mypy-tests.ini`, and the acceptance
   exercise with `EVENT_ARB_ACCEPTANCE_OUT` pointing to a retained evidence directory.
   Report actual results and remaining limitations. Update the readiness, handoff, runbook
   and launch-package documents so their claims match the new evidence. Deliver one concise
   remaining-input checklist and exact commands for the first 30–60-minute supervised run.

Complete the engineering and offline verification autonomously. This prompt authorizes
local implementation and offline tests, not production venue requests or a live-data run.
Keep real-order submission structurally disabled. Do not enable Global live access, add
venues, implement WebSockets, widen discovery, auto-approve a pair or auto-settle outcomes.
Do not invent operator eligibility, current rules, fee evidence or market IDs. Prepare the
concrete launch package first; then identify only the exact operator inputs/authorization
still missing. A no-trade run may establish healthy operation but does not establish fill
quality, profitability or settlement. If there is no equivalent pair, say so clearly.
