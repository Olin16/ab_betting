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
| 7 | Supervised live read-only smoke test | **Attempted 2026-08-26; failed after 2 catalogue requests; book criteria unmeasured** (`docs/smoke-test-plan.md`) |
| 8 | Unattended paper trial | Blocked on step 7 evidence and the human gates below. F2/F5/F6 are done |

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

## What remains before a trial

**Pagination** is still unimplemented, which is acceptable while trials name their markets
explicitly but blocks any move to venue-wide discovery.

**Step 6 — hardening.** Compose publishes both the dashboard and Postgres on `127.0.0.1`
only. Binding the API to a non-loopback interface without `EVENT_ARB_API_AUTH_TOKEN` now
fails at startup rather than quietly exposing the approval endpoint, and state-changing
routes require a bearer token compared in constant time. Read-only health and metrics stay
open for monitoring. Degraded ingestion raises a loud `trial.adapter_unhealthy` alert,
because an unattended trial that stops receiving data otherwise produces a tidy report of
nothing.

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

Two limits are worth restating, because they are what "done" means here rather than
qualifications on it:

- no paper trade has yet produced `measured` leg-2 evidence, and none can from fixtures —
  it requires a live venue returning a book that moved (`docs/operations.md`);
- `paper.leg2_adverse_model_pct` has no calibrated value, so the modelled path is available
  but unused.

## Human gates

- [ ] Complete Polymarket US onboarding, current terms review, state-eligibility check,
      and any required written production API/conformance approval.
- [ ] At least one real pair approved by a human who read both rulebooks end to end.
- [ ] Kalshi membership in good standing; Rulebook permits the intended automated activity.
- [ ] Kalshi authentication verified against a real key (api-assumptions A5). A4 is
      resolved: public market-data REST returns 200 without any credential on both venues,
      verified 2026-08-23. Only the WebSocket handshake requires authentication, which is
      why `ingestion_mode` defaults to `rest_polling`.
