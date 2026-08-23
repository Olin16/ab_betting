# Trial Readiness — Status

Tracks the agreed plan toward a supervised smoke test and then an unattended paper trial.
**Live trading remains out of scope and structurally impossible.**

| # | Step | Status |
| --- | --- | --- |
| 1 | Review venue terms and developer agreement | **Done** (Kalshi); Polymarket outstanding |
| 2 | Watched-pair configuration and targeted refresh | **Done** |
| 3 | Persistence and safe approval restore | **Done** |
| 4 | Continuous synchronization; resolve sequence behaviour | **Partial** — sequencing resolved, continuous ingestion not wired |
| 5 | Continuous scanning and automated paper execution | **Not started** |
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

## What remains before a trial

**Step 4 (ingestion half).** `BookSynchronizer` is still not started by the application
lifespan; the running server performs one REST refresh and then goes passive. Continuous
WebSocket ingestion with periodic REST resynchronization needs wiring in, with clean task
shutdown.

**Step 5 — the evidence pipeline.** This is the substantive remaining gap. There is still
no autonomous loop: `scan()` and `paper_trade()` are only invoked by a CLI command or an
HTTP request. Without periodic or event-driven scanning feeding automatic paper execution
and persisted results, the system can observe prices but cannot accumulate longitudinal
evidence about fill failures, latency, partial fills, unhedged exposure, or missed
opportunities — which is the entire purpose of the trial. `trial.scan_interval_seconds`,
`trial.refresh_interval_seconds` and `trial.auto_paper_trade` are configured but not yet
consumed.

The paper book provider in `scanner.py` also still ignores the configured latency and
returns the current snapshot. Real latency handling or timestamped book replay is needed
for the fill-failure evidence to mean anything.

**Step 6 — operational hardening.** Docker still binds `0.0.0.0` and publishes Postgres
with a default password; the dashboard has no authentication and its approval endpoint is
state-changing and unauthenticated. Health alerting is not wired.

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
