# Operations

> **Venue status.** Polymarket **Global** is legacy and is never a US fallback.
> Polymarket **US** (QCX LLC) is implemented for **public REST, read-only, credential-free**.
> Its WebSocket is on the authenticated API and is **not** implemented. Both venues serve
> market data over unauthenticated REST but authenticate their WebSocket handshake even for
> public channels. No real order submission exists. Research candidates are not arbitrage.
> Public endpoint availability does not resolve terms, KYC, eligibility, retention or
> production-approval questions.


## Running

```bash
uv venv --python 3.13
uv pip install -e ".[dev]"
cp .env.example .env

.venv/bin/event-arb doctor    # config + verify live trading is impossible
.venv/bin/event-arb scan      # one-shot detection
.venv/bin/event-arb pairs     # candidate pairs with rule differences
.venv/bin/event-arb paper     # scan + simulated two-leg execution
.venv/bin/event-arb serve     # dashboard on 127.0.0.1:8000
```

### Seeing the pipeline end to end

`--replay-recorded-reviews` on `scan`/`paper` replays the human reviews recorded in
`tests/fixtures/recorded_reviews.json`, so an approved pair exists and the confirmed path
can be demonstrated. It **refuses to run against anything but fixture data**, and the check
happens before any adapter is built, so a live invocation makes no request at all.

It is not automated approval. It matches exact fixture market ids against reviews a person
wrote down; it never consults the rule comparison to decide anything, and the recorded
sources go through the ordinary approval path, which still applies every readiness check
and still refuses a pair that is not ready.

There was previously an `--auto-approve` flag that turned "the automated rule comparison
found no material difference" into `manually_approved = True`, and it worked on live data.
It has been removed. Automated text comparison cannot establish settlement equivalence —
only a human who has read both rulebooks can — and `tests/unit/test_no_automated_approval.py`
now asserts at AST level that `matching/approval.py` is the only module in the codebase
that can set that flag.

With Docker: `docker compose up` starts PostgreSQL, runs migrations, and serves the
dashboard on port 8000 using fixture data.

## Migrations

```bash
alembic upgrade head
alembic revision --autogenerate -m "description"   # review before committing
```

The URL comes from `EVENT_ARB_DATABASE_URL`, never from `alembic.ini`, so a production
credential is never written into a tracked file.

**Never run migrations against a database you are not prepared to change.** `alembic
upgrade head` with no `EVENT_ARB_DATABASE_URL` set targets `data/event_arb.db`.

### Migration coverage

The chain is exercised on **both** engines the project supports:

| | SQLite | PostgreSQL 16.15 |
| --- | --- | --- |
| full chain up / down / up | yes (11 automated tests) | yes, verified 2026-08-24 |
| seeded pre-migration rows | yes | yes |
| foreign keys enforced | no (SQLite default) | **yes** |

PostgreSQL is where `8c41f0b7d2ae` is actually tested: it enforces
`opportunities.pair_id -> market_pairs.id`, so the insert-new / repoint-children /
delete-old ordering has to be right. It also caught a defect SQLite cannot — a literal
`manually_approved = 0`, which SQLite accepts for a boolean column and PostgreSQL rejects.

The automated tests run on SQLite only, because the suite must not require a database
server. The PostgreSQL run is manual; repeat it before any migration reaches a PostgreSQL
deployment.

## Data retention

Retained:
- normalized market metadata (title, rules text, close time, settlement source, fees);
- normalized order-book snapshots, for replay and audit;
- derived opportunities, paper trades, and the audit log.

Not retained:
- raw API payloads beyond a bounded debug buffer (off by default);
- credentials, signatures, or auth headers, in any form;
- the caller IP returned by the **Polymarket Global** geoblock endpoint. Polymarket US
  uses separate onboarding/KYC and API eligibility controls.

Nothing is redistributed. **Both venues' developer terms may restrict storage and
redistribution.** The Kalshi Developer Agreement and Polymarket Global terms have been
reviewed; the complete current Polymarket US participant and API terms remain a human gate.
Set a retention window for `orderbook_snapshots` before running against production feeds;
it is the fastest-growing table by far.

## Rate limits

Documented per-tier limits were not verified for this build. Clients apply a conservative
token bucket (default 5 req/s per venue) and honour `Retry-After` on 429. Confirm the real
limits for your access tier before pointing this at production. The limiter is a term of
use, not a performance knob.

### Request budget

Paper trading is not free of venue traffic. In REST-polling mode each simulated trade
issues **two extra order-book requests** — one per leg, at that leg's simulated arrival
time — on top of scan traffic. They go through the same token bucket, so they consume the
same budget; they are counted in `event_arb_paper_refetches_total{venue}`.

Budget accordingly:

    requests/minute ≈ (watched markets × scans/minute) + (2 × paper trades/minute)

These requests are not optional padding. Re-reading the cached snapshot instead would hand
leg 2 the same book the opportunity was detected against, which makes hedge failure
impossible by construction and produces a 100% hedge rate that means nothing. In streaming
mode the store is already being updated, so no extra request is issued.

## Monitoring

`/health` — adapter connectivity, freshness, reconnects, sequence gaps, and
`live_trading_possible` (always `false`).

`/metrics` — Prometheus text. Alert-worthy series:

| Metric | Watch for |
| --- | --- |
| `event_arb_venue_up` | 0 for more than a minute |
| `event_arb_ws_reconnects_total` | sustained increase |
| `event_arb_resyncs_total` | frequent gaps: the feed is unreliable |
| `event_arb_book_age_ms` | p99 approaching `max_book_age_ms` — see the note below |
| `event_arb_opportunities_rejected_total{reason="book_stale"}` | data-quality problem |
| `event_arb_paper_trades_total{status="leg2_failed"}` | the risk that matters most |
| `event_arb_paper_refetches_total` | paper traffic against the rate-limit budget |
| `event_arb_paper_trade_evidence_total{grade="measured"}` | should be ~all trades; see below |

### Setting max_book_age_ms

The bound must be at least what one refresh actually costs. `event-arb doctor` prints the
required value:

    required book age = worst refresh batch + clock tolerance

The cycle interval is **not** a term. The runtime refreshes, persists, then scans within
one cycle, and skips the scan outright when the refresh fails, so no book survives into a
later cycle's scan. The oldest book a scan can see is the first one fetched in the batch
immediately before it.

An earlier version of this formula added a whole cycle, demanding roughly 6.3 s where
reality was 1.3 s. That reported the default 2 s bound as TOO TIGHT and made raising
`max_book_age_ms` look like the fix — loosening the check that catches genuinely stale
prices. **If doctor says the budget is too tight, shrink the refresh batch (watch fewer
markets) before touching the bound.**

### Reading the hedge success rate

`hedge_success_rate_pct` is only meaningful when the leg-2 book was re-observed **and had
moved**. Every trade is graded:

| Grade | Meaning | Supports a hedge claim? |
| --- | --- | --- |
| `measured` | leg 2 priced against a book observed to have moved | yes |
| `unchanged_reread` | the venue was asked again and returned the same book | **no** |
| `modelled` | leg 2 priced against a configured synthetic adverse move | **no** |

Fixture replay is always `unchanged_reread` — the file cannot move. A 100% hedge rate over
`unchanged_reread` trades shows only that the book did not move in that sample; it is not
evidence that hedging survives adverse movement, and the daily report prints that caveat
next to the number. Do not carry such a rate into the live-execution checklist below.

`paper.leg2_adverse_model_pct` applies a synthetic adverse move to leg 2 so fixtures can
exercise the failure path. It requires `paper.leg2_adverse_model_source` naming the
observation it was calibrated from (venue, market, date range, sample size) — an
uncalibrated rate is a guess, and a guess that flatters the strategy is indistinguishable
from evidence once it reaches a report. Trades run under it are graded `modelled`.

## Dashboard exposure

The dashboard has **no authentication** and binds to `127.0.0.1` by default. The JSON API
includes a state-changing, unauthenticated approval endpoint. Do not expose it to a
network without an authenticating reverse proxy.

---

# Checklist for safely considering live execution later

Nothing below is implemented. This is the gate, in order. **Do not start at step 4.**

## A. Evidence from this build

- [ ] Run the scanner against live *read-only* data for a sustained period (weeks, not days).
- [ ] Paper-trade continuously over the same period.
- [ ] Review `hedge_success_rate`: what fraction of attempts actually ended fully hedged?
- [ ] Review `realization_rate`: how much of the *detected* profit survived simulated
      execution? If it is far below 100%, the edge is a modelling artifact. Values above
      100% are expected on clean fills, because detection subtracts safety buffers that a
      clean fill does not actually incur.
- [ ] Count `leg2_failed` and `partially_hedged` outcomes and price what those unhedged
      positions would have cost at real settlement.
- [ ] Confirm at least one full settlement cycle reconciled as predicted.
- [ ] Confirm no confirmed opportunity was ever produced from a stale or gapped book.

## B. Verify every API assumption

- [ ] Re-read `docs/api-assumptions.md` end to end; confirm each item against current docs.
- [ ] **Confirm the Kalshi signing message format** (A5) against a real authenticated call.
- [ ] Confirm whether market data requires authentication (A4).
- [ ] Confirm the current fee schedule: coefficient, per-series multipliers, maker rates,
      and your member balance precision (A7).
- [ ] Confirm the selected Polymarket product's current fee coefficient, rebates, rounding,
      tick sizes, and quantity constraints. Global B4 does not apply to Polymarket US.
- [ ] Confirm real rate limits for your access tier (C2).
- [ ] Run `pytest -m live` and confirm no schema assertion fails.

## C. Legal and compliance

- [ ] Read the Kalshi Developer Agreement in full; confirm automated trading is permitted
      for your account type.
- [ ] Read the terms for the selected Polymarket product. A US operator must use
      Polymarket US, complete KYC, confirm state eligibility, and obtain any required
      production API/conformance approval.
- [ ] Confirm KYC is complete and in good standing on both venues.
- [ ] Confirm jurisdiction through the selected product's official process. The geoblock
      endpoint applies to Global; Polymarket US uses its own onboarding and eligibility
      policy. Never use a workaround.
- [ ] Understand the tax treatment of both instruments.
- [ ] Obtain written sign-off if trading on behalf of anyone else.

## D. Market pairs

- [ ] Every pair intended for live use is approved by a **named human** who read both
      complete rulebooks — not the automated diff.
- [ ] Material differences are documented with an explicit rationale for accepting them.
- [ ] Rules hashes are stored and the auto-suspension path has been tested in anger.

## E. Security (before any key touches this system)

- [ ] Commit a hash-pinned lockfile; enable Dependabot and `pip-audit` in CI.
- [ ] Add pre-commit secret scanning (gitleaks or equivalent).
- [ ] Store keys outside the repo, `0600`, ideally in an OS keychain or secrets manager.
- [ ] Implement `Signer` against a hardware or remote signer — never a raw key in process
      memory if avoidable.
- [ ] Use separate credentials per venue with the narrowest permissions available.
- [ ] Confirm no withdrawal or transfer permission is granted to any API key.
- [ ] Put the dashboard behind authentication; add CSRF protection to state-changing routes.
- [ ] Re-audit logs at `DEBUG` for anything sensitive.

## F. Implementation (Phase 7 itself)

- [ ] `TradingEnabled` stays `false` by default and requires a second, explicit
      confirmation at startup.
- [ ] Product-specific compliance checks pass before every submission: Global requires an
      affirmative geoblock result; Polymarket US requires a valid KYC/eligible account and
      any venue-mandated authorization checks. All checks fail closed.
- [ ] Idempotent client order IDs on every request; retries reuse the same ID.
- [ ] After **any** timeout, resolve true order state by querying the venue. Never assume
      failure — assuming failure and retrying is how you double a position.
- [ ] Order-size, daily-loss, total-exposure, and minimum-balance limits enforced.
- [ ] Circuit breaker on consecutive failures; kill switch reachable without a deploy.
- [ ] Unwind logic for a failed second leg, with an explicit maximum acceptable loss.
- [ ] Manual per-trade approval required before any autonomous mode.
- [ ] Append-only audit log with redaction for every submission, fill, and cancellation.
- [ ] No automatic withdrawal or transfer capability, ever.

## G. Rollout

- [ ] Run against the Kalshi **demo** environment first.
- [ ] First live run: one pair, minimum size, manual approval on every trade.
- [ ] Reconcile every fill and every settlement by hand for the first week.
- [ ] Only then consider raising size — one limit at a time.

**Stop condition:** if any item in A or B cannot be satisfied, do not proceed. An
unverified fee schedule or an unproven hedge rate turns "arbitrage" into leveraged
directional risk with extra steps.

---

## Candidate research

`event-arb candidates discover` proposes cross-venue pair *candidates* from public market
metadata. It never approves, never paper-trades, never reads an order book, and never
opens a WebSocket.

```bash
event-arb candidates discover \
  --kalshi-limit 250 --polymarket-limit 250 \
  --min-score 0.55 --max-per-market 3 \
  --max-results 100 --recheck-top 25
```

Before any request it prints a plan **derived from the adapters that will serve it** —
endpoints, parameters, whether credentials are sent, minimum and worst-case request counts,
pagination behaviour, and what will be retained. In live mode it then waits for you to type
`yes`, unless `--yes-i-authorize-live-requests` is passed.

| Flag | Effect |
| --- | --- |
| `--recheck-top N` | asks the venue whether the top N shortlisted ids still resolve |
| `--authenticated-mode` | authorises sending configured venue credentials |
| `--yes-i-authorize-live-requests` | skips the interactive confirmation |

**If venue credentials are configured, the command refuses to run** unless
`--authenticated-mode` is given, because the plan it displays is the credential-free one
and an authorisation based on a false plan is worthless.

`--recheck-top` is an **existence check**. It re-requests the ids and discards the response
body: nothing is re-normalized, no score is recomputed, no rule comparison is re-run, no
fingerprint recalculated, no readiness check repeated. Records carry `existence_rechecked`
and `existence_rechecked_at` rather than "verified", because calling it verification
invited a reviewer to skip exactly the work it does not do.

Bounds: one page per venue; a returned cursor is counted and reported but **never
followed**; source catalogues stay in memory and only the capped shortlist is persisted;
existing human dispositions (approved, rejected, suspended, expert review, under review)
are read verbatim and **never written back**. The command persists only pairs that do not
already exist, so it has no way to alter a decision a human recorded.

### Request cost of REST paper trading

Live REST paper trading fetches a **fresh snapshot at each simulated leg's arrival**, so a
paper trade costs two additional rate-limited market-data requests beyond the polling
cycle. Without that, leg 2 would re-read leg 1's snapshot and every fill would appear
clean. See `docs/risk-model.md`.
