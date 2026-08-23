# Trial Readiness — Status

Tracks the agreed plan toward a supervised smoke test and then an unattended paper trial.
**Live trading remains out of scope and structurally impossible.**

| # | Step | Status |
| --- | --- | --- |
| 1 | Review venue terms and developer agreement | **Done** (Kalshi); Polymarket outstanding |
| 2 | Watched-pair configuration and targeted refresh | **Done** |
| 3 | Persistence and safe approval restore | **Done** |
| 4 | Continuous synchronization; resolve sequence behaviour | **Done** |
| 5 | Continuous scanning and automated paper execution | **Done** |
| 6 | Retention and operational hardening | **Partial** — retention done; hardening outstanding |
| 7 | Supervised live read-only smoke test | Blocked on 4–6 |
| 8 | Unattended paper trial | Blocked on 4–6 |

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

**Step 6 — operational hardening.** Docker still binds `0.0.0.0` and publishes Postgres
with a default password; the dashboard has no authentication and its approval endpoint is
state-changing and unauthenticated. Health alerting is not wired.

**Pagination** is still unimplemented, which is acceptable while trials name their markets
explicitly but blocks any move to venue-wide discovery.

## Review fixes applied

- Approvals now require **freshly observed** metadata for both legs; cached metadata
  cannot verify an approval because it matches its own stored hash by construction.
- The approval fingerprint is **versioned** and covers close time, outcome set and
  settlement asset, not just prose.
- Discovery is scoped to watched markets, not only order-book refresh.
- Malformed WebSocket updates now emit an immediately stale book on both venues, matching
  sequence-gap handling. Previously the stored book stayed eligible until it aged out.

## Human gates

- [ ] **Polymarket Terms of Use** — still unreviewed. The page is JavaScript-rendered and
      could not be machine-read. Needs a human before any live access.
- [ ] At least one real pair approved by a human who read both rulebooks end to end.
- [ ] Kalshi membership in good standing; Rulebook permits the intended automated activity.
- [ ] Kalshi authentication verified against a real key (api-assumptions A5), and whether
      market data requires auth at all (A4).
