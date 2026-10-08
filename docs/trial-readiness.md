# Trial Readiness — Status

> **Venue status.** Polymarket **Global** is legacy and is never a US fallback.
> Polymarket **US** (QCX LLC) is implemented for **public REST, read-only, credential-free**.
> Its WebSocket is on the authenticated API and is **not** implemented. Both venues serve
> market data over unauthenticated REST but authenticate their WebSocket handshake even for
> public channels, so REST polling is the credential-free ingestion path.
> No real order submission exists. Research candidates are not arbitrage. Public endpoint
> availability does not resolve terms, KYC, eligibility, retention or production-approval
> questions.


Tracks the agreed plan toward a supervised smoke test and then an unattended paper trial.
**Live trading remains out of scope and structurally impossible.**

| # | Step | Status |
| --- | --- | --- |
| 1 | Review venue terms and developer agreement | **Done for technical design** — operator eligibility/permission confirmations remain human gates |
| 2 | Watched-pair configuration and targeted refresh | **Done** |
| 3 | Persistence and safe approval restore | **Done** |
| 4 | Continuous synchronization; resolve sequence behaviour | **Done** |
| 5 | Continuous scanning and automated paper execution | **Done** |
| 6 | Retention and operational hardening | **Done** |
| 7 | Supervised live read-only smoke test | **Attempted 2026-08-26; failed after 2 catalogue requests; book criteria unmeasured** (`docs/smoke-test-plan.md`). Cause diagnosed offline and fixed in local scoring (`docs/zero-candidate-diagnosis.md`); a re-run is proposed, not approved |
| 8 | Unattended paper trial | **Engineering complete; not yet run.** Durable accounting, feed-coverage gating, the run journal, independent trade/ledger reconciliation, the Global **and** US offline acceptance exercises, the executable launch procedure and the runbook are done (see below). Still blocked on step 7 evidence and the human gates at the foot of this page |

## What is done

**Step 1.** Kalshi Developer Agreement read in full. §3 limits API use to facilitating your
own trading; §3.1 forbids sharing API data with third parties without written
authorization. Webhook alerts are now fail-closed and carry no venue market data by
default. §3.5 forbids availability monitoring and benchmarking. §4.1.1 requires exchange
membership. Rate limits verified (Basic: 200 read tokens/s, ~20 req/s). See
`docs/compliance.md`.

**Step 2.** Book refresh is scoped to watched pairs, so request count scales with pairs
watched rather than venue size. Configuration can select what to watch but can never
approve a pair.

**Step 3.** Markets, pairs, approval metadata, rule hashes, opportunities, paper trades,
book snapshots and an append-only audit log all persist. Restoration re-verifies every
approval against **current** rules and suspends any that no longer match, or whose market
metadata cannot be read. Verified end-to-end: an approval survives a restart unchanged,
and is suspended and audited when the venue amends its rules while the process is down.

**Step 4 (sequencing half).** Each market gets its own subscription command, multiplexed
over one connection, with state keyed by the assigned `sid`. This removes the per-market
vs per-subscription `seq` ambiguity: a `sid` now names exactly one market, so the counter
is unambiguous under either interpretation. `use_yes_price` is pinned explicitly.

**Steps 4–5 — the autonomous pipeline.** `TrialRunner` (`src/event_arb/runtime.py`) owns
four cooperating tasks: REST refresh, opportunity scanning, paper execution, and
maintenance (snapshot pruning plus metadata re-fetch so a mid-trial rules change is caught
without waiting for a restart). WebSocket ingestion runs via `BookSynchronizer` in live
mode. Failures in one loop are recorded and survived rather than taking the run down, and
`stop()` cancels and awaits every task.

Paper fills are now **latency-aware**: the book provider actually waits the configured
network latency before reading, so with a live feed leg 2 arrives into a book that has
genuinely moved. Reading the detection-time snapshot instead would make every fill look
clean and understate exactly the fill-failure risk the trial exists to measure.

Run it with `event-arb trial` (add `--duration` to time-box, `--rest-only` to skip
WebSockets), or set `EVENT_ARB_TRIAL__ENABLED=true` and use the dashboard, where
`/api/trial` exposes live counters.

Automation deliberately does not widen scope: unapproved pairs are not paper-traded unless
`trial.paper_trade_research_candidates` is explicitly set, and even then they are never
labeled arbitrage.

## Step 8 engineering — what was completed on 2026-09-10

The September 10 review found the working tree **not ready** for an unattended trial and
named a reproduced accounting defect as the blocker. That work is now done. What changed:

**Durable-first publication.** `Scanner.paper_trade` used to debit cash and append the
trade to the portfolio *before* the transaction that stored them. With the trade write
failing, memory held one trade and balances of `kalshi:USD=9951.2500` /
`polymarket_us:USD=9960.5900`; reopening the same database showed zero trades and
`10000.0000` in both accounts — and the restore report called that complete. Nothing is
published to memory now until storage has accepted it, so the two agree by construction.
A write that will not land invalidates the attempt: it is recorded, counted, excluded from
every rate, and the run stops. See `src/event_arb/paper/durability.py`.

**A leg-1 checkpoint before leg 2.** Recording the exposure in an `except` clause is a
recovery path that assumes the failure it recovers from did not happen — a process killed
outright between the legs never reaches it. The exposure is now written *before* leg 2 is
attempted.

**Cancellation stays cancellation.** An operator's Ctrl-C between the legs is handled,
stored, and then re-raised as the cancellation it was, rather than being rewritten into an
execution failure with a spurious consecutive-failure count and a non-zero exit.

**Reconciliation.** `event-arb reconcile` re-derives every account from the stored ledger
and checks `opening + movements == ending` per account, exiting non-zero when the books do
not close or when an attempt was invalidated. `event-arb trial` prints the same
reconciliation at the end of every run and fails the run if it does not close.

**Offline acceptance evidence.** `tests/acceptance/test_offline_trial_acceptance.py` drives
the assembled `TrialRunner` through a dead feed, a failing scan, storage refusing a trade
write, capital depletion, an interruption between the legs, an abrupt stop with the
interruption handler disabled, and a settlement write that fails and is repaired across a
restart — reconciling every account from storage after each.

**Configuration and runbook.** `config/trial.env.example` pins the posture, scope, cadence,
capital, limits and watchdog thresholds for a REST-only Kalshi + Polymarket US trial with
deliberately unusable placeholder market ids. `docs/paper-trial-runbook.md` is the
procedure, including exit codes, the request budget, the settlement and reconciliation
steps, and how to report a no-trade run honestly.
`docs/live-trial-launch-package.md` is what an operator reviews before authorizing one: it
separates what is ready from what only a person can supply, and carries the templates for
the pair review and the approval record.

## Step 8 engineering — what was completed on 2026-09-22

A second review of the same working tree reproduced three further gaps. None of them was a
missing feature; each was a check that returned the right-looking answer for the wrong
reason. All three are fixed, each with a regression test that fails against the old code.

**A dead feed was reported as a quiet market.** `Scanner.refresh_books` catches each
`fetch_orderbook` failure, marks that book stale and carries on — so a total venue outage
and a healthy cycle both ended as "returned a list, raised nothing". Every consumer read
that silence as success. Reproduced by replacing both adapters' `fetch_orderbook` with a
raising function: **preflight passed, 62 refreshes were counted with zero errors, no stop
reason, and `runner.failed` was `False`**.

Refresh now returns a `RefreshOutcome` describing per-market coverage — which required
markets came back usable, which failed, which returned a book with no executable depth, and
which were skipped. Preflight blocks a trading run on incomplete coverage or on a freshly
fetched book that is already stale; the runtime counts an incomplete batch as a refresh
failure and skips the scan that would otherwise record a confident "no opportunities"
against books it does not have. The health loop now escalates sustained degradation to a
stop, which its docstring had promised and its code never did, and a health check that
itself keeps raising is bounded rather than looping forever. `TrialStats` gained
`refreshes_incomplete` and `feed_was_healthy`. See `src/event_arb/orderbooks/coverage.py`.

**A restart upgraded incomplete evidence to complete.** Invalidation lived only in a Python
list. Before closing, a run that lost an attempt reported `invalidated=1` and
`is_complete_evidence=False`; **reopening the same database reported `invalidated=0` and
`is_complete_evidence=True`**. Writing the invalidation to the database cannot fix this —
the attempt is invalidated precisely *because* the database refused it, and a failure record
that depends on the failed subsystem is the same silence with more steps.

So there is now an append-only, `fsync`'d journal beside the database
(`src/event_arb/paper/journal.py`). It carries invalidated attempts and, separately,
*unfinished runs*: a run appends a marker when it opens and only an orderly shutdown closes
it, so a start with no finish is the one trace a killed process leaves. Both fail closed.
A repair is recorded as its own `attempt_resolved` entry rather than by deleting the
failure, and an operator clearing an unfinished run does so with
`event-arb acknowledge-run <id> --by … --note …`, which appends — the crash record is never
removed, so the gate going green always leaves a named person and a reason behind.

**Reconciliation was re-adding the ledger to itself.** `opening + movements == ending` has
both sides computed from the same ledger, so deleting a trade's `trade_debit` rows removes
them from both at once and the identity still holds. Reproduced: one trade retained, its two
debits deleted, **Kalshi cash rose from 9943.2600 to 9998.2600 and the report still said
`reconciles=True`**.

The trade rows are stored separately from the ledger, so they are the second source of
truth. `src/event_arb/paper/integrity.py` re-derives each retained trade's implied entries
and checks the ledger contains them, naming missing, wrong-amount, wrong-account,
unexpected and orphaned movements. Retention compaction is respected through a boundary
read from the carry-forward entries themselves: a trade below it is reported as *compacted
and not verified* rather than as corrupt.

`event-arb reconcile` is also genuinely read-only now. It used to call the restoring path,
which reaps stale holds **and persists the release** — so inspecting a database mutated it,
and inspecting one while a trial was running released the reservations that trial was
using. `Scanner.inspect_portfolio` is the non-mutating counterpart; only `trial` recovers.

**A real process kill is now tested.** `tests/integration/test_abrupt_termination.py`
spawns a subprocess, lets it open a run and take on real exposure, sends `SIGKILL`, and
asks a fresh process what it can still tell. The journal marker survives, the run reports
as unfinished, the leg-1 checkpoint is intact, and the evidence gate fails closed. The
runbook's previous "we simulate the process boundary rather than crossing it" limitation no
longer applies to this path.

**The deployment target is proved offline.** `tests/acceptance/test_us_trial_acceptance.py`
runs Kalshi + Polymarket **US**, USD on both legs, through the assembled runtime: fresh
schema migration, approval persistence across a restart, startup preflight, a healthy
end-to-end run, funded execution debiting the two USD accounts separately, interruption and
restart, settlement with aligned **and inverted** outcomes, an adapter-level outage, and an
unfinished run. The Global file is retained as legacy coverage rather than as the proof for
a trial that will not use Global.

**The launch procedure is executable.** `config/trial.env` is ignored by an exact
`.gitignore` rule; the pair review page prints an authenticated approval command with
`curl -fsS` (without `-f`, curl exits zero on a 401, so a refused approval looked accepted);
`EVENT_ARB_REVIEW_ONLY=1` serves a dashboard that cannot start a trial, which matters
because the example configuration enables one; `scripts/run-paper-trial.sh` captures the
trial's real exit code via `PIPESTATUS` rather than tee's; and `event-arb manifest` hashes
source **content**, including untracked files, excluding secrets by name.

The request budget was also wrong and is now measured in
`tests/integration/test_request_budget.py`: one pair costs **one** request per venue per
cycle, so ≤ 720 per venue per hour at a 5 s cadence — not 1,440 — plus two per simulated
trade. Polymarket Global costs two GETs per book; that is the legacy path, not this trial's.

## What remains before a trial

**Pagination** is still unimplemented, which is acceptable while trials name their markets
explicitly but blocks any move to venue-wide discovery.

**Step 6 — hardening.** Compose publishes both the dashboard and Postgres on `127.0.0.1`
only. Binding the API to a non-loopback interface without `EVENT_ARB_API_AUTH_TOKEN` now
fails at startup rather than quietly exposing the approval endpoint, and state-changing
routes require a bearer token compared in constant time. Read-only health and metrics stay
open for monitoring. Degraded ingestion raises a loud `trial.adapter_unhealthy` alert,
pauses trading, and — since 2026-09-22 — **stops the run** if it persists, because an
unattended trial that stops receiving data otherwise produces a tidy report of nothing.
An adapter-level outage is caught too: refresh reports per-market coverage rather than
returning normally whether or not any book arrived.

## Review fixes applied

- Approvals now require **freshly observed** metadata for both legs; cached metadata
  cannot verify an approval because it matches its own stored hash by construction.
- The approval fingerprint is **versioned** and covers close time, outcome set and
  settlement asset, not just prose.
- Discovery is scoped to watched markets, not only order-book refresh.
- Malformed WebSocket updates now emit an immediately stale book on both venues, matching
  sequence-gap handling. Previously the stored book stayed eligible until it aged out.

## ⚠ Polymarket product pivot

The reviewed terms govern **Polymarket Global**. They prohibit a US operator from using
that product's CLOB technology, so the existing live adapter is not a lawful US trial
target. Polymarket US is a separate QCX-operated, CFTC-designated product with a documented
public API, authenticated market WebSocket, official SDKs, and USD settlement.

The scanner has pivoted to Polymarket US. Its public read-only REST adapter is implemented and credential-free; its WebSocket is authenticated and is not.
Configuration distinguishes US from Global and fails closed: selecting US cannot silently
construct the Global client, while selecting Global requires an explicit eligibility and
permission acknowledgement.

The US adapter is implemented and fixture-tested. Before step 7, complete US onboarding and
terms review, confirm state eligibility and any production API/conformance approval, and
verify the current US fee schedule. No technical workaround or Global fallback is acceptable.
Full detail is in `docs/compliance.md` and `docs/venue-roadmap.md`.

## Credential requirements (resolved)

A read-only paper trial needs **no credentials on either venue**, provided ingestion is
REST polling:

| | REST market data | WebSocket |
| --- | --- | --- |
| Kalshi | public, no key | **authenticated handshake, even for public channels** |
| Polymarket US | public gateway, no key | authenticated API only |

`ingestion_mode` defaults to `rest_polling`. Selecting `websocket` fails closed rather than
starting a stream that cannot authenticate, and the dashboard no longer infers streaming
from merely running in live mode.

## Remediation status

F1–F10 from the readiness review are implemented and each has a regression test. F2 (fee
policy persistence), F5 (product in every identity) and F6 (honest execution evidence) were
the three code findings that blocked step 8; they no longer do. Step 8 still requires step
7 evidence plus the operator confirmations below. Those are human gates rather than code
gaps.

Three limits are worth restating, because they are what "done" means here rather than
qualifications on it:

- no paper trade has yet produced `measured` leg-2 evidence, and none can from fixtures —
  it requires a live venue returning a book that moved (`docs/operations.md`);
- `paper.leg2_adverse_model_pct` has no calibrated value, so the modelled path is available
  but unused;
- no live-data trial has been run, so every figure the paper machinery has produced to date
  comes from fixtures. The engineering is verified; the trial is not evidence until it runs;
- SQLite durability under power loss, or on a filesystem that lies about `fsync`, is not
  covered by anything here. The `SIGKILL` test crosses the process boundary on this host's
  filesystem; it does not establish the general case, and should not be quoted as doing so.

## Human gates

- [ ] Complete Polymarket US onboarding, current terms review, state-eligibility check,
      and any required written production API/conformance approval.
- [ ] At least one real pair approved by a human who read both rulebooks end to end.
- [ ] Kalshi membership in good standing; Rulebook permits the intended automated activity.
- [ ] Confirm the real rate limits for **your** access tier. The 5 req/s client ceiling is
      a conservative configured guess, not a measured entitlement, and the measured budget
      is ≤ 720 requests per venue per hour for one pair at a 5 s cadence.

**Not a gate for this trial:** Kalshi request signing against a real key
(api-assumptions A5). It applies only to authenticated endpoints, and this trial touches
none — it is REST-only and credential-free by design. A4 is resolved: public market-data
REST returns 200 without any credential on both venues, verified 2026-08-23. Only the
WebSocket handshake requires authentication, which is why `ingestion_mode` defaults to
`rest_polling`. A5 becomes a gate if and when authenticated WebSocket ingestion is taken
up, which is deliberately deferred.
