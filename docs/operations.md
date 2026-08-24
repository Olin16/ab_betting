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

`--auto-approve` on `scan`/`paper` is a **demo affordance only**: it approves candidates
that show no material differences, so the pipeline can be seen end to end. It is not a
substitute for a human reading both rulebooks, and it deliberately refuses any pair with
material differences.

With Docker: `docker compose up` starts PostgreSQL, runs migrations, and serves the
dashboard on port 8000 using fixture data.

## Migrations

```bash
alembic upgrade head
alembic revision --autogenerate -m "description"   # review before committing
```

The URL comes from `EVENT_ARB_DATABASE_URL`, never from `alembic.ini`, so a production
credential is never written into a tracked file.

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

## Monitoring

`/health` — adapter connectivity, freshness, reconnects, sequence gaps, and
`live_trading_possible` (always `false`).

`/metrics` — Prometheus text. Alert-worthy series:

| Metric | Watch for |
| --- | --- |
| `event_arb_venue_up` | 0 for more than a minute |
| `event_arb_ws_reconnects_total` | sustained increase |
| `event_arb_resyncs_total` | frequent gaps: the feed is unreliable |
| `event_arb_book_age_ms` | p99 approaching `max_book_age_ms` |
| `event_arb_opportunities_rejected_total{reason="book_stale"}` | data-quality problem |
| `event_arb_paper_trades_total{status="leg2_failed"}` | the risk that matters most |

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
