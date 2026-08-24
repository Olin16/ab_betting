# event-arb — Implementation Plan

> **Venue status.** Polymarket **Global** is legacy and is never a US fallback.
> Polymarket **US** (QCX LLC) is implemented for **public REST, read-only, credential-free**.
> Its WebSocket is on the authenticated API and is **not** implemented. Both venues serve
> market data over unauthenticated REST but authenticate their WebSocket handshake even for
> public channels. No real order submission exists. Research candidates are not arbitrage.
> Public endpoint availability does not resolve terms, KYC, eligibility, retention or
> production-approval questions.


## Objective

Detect *potential* arbitrage between economically equivalent binary event contracts on
**Kalshi** and **Polymarket US**, with market-data collection, opportunity detection and
paper trading only. The existing international Polymarket implementation is legacy and
must not be confused with the separate US product. Live order submission is out of scope
and structurally disabled.

## Non-goals (explicit)

- No live order submission (Phase 7 interfaces only; not implemented, not enabled).
- No circumvention of geoblocking, KYC, rate limits, or venue terms.
- No claim that any opportunity is "risk-free". Cross-venue execution is not atomic.

## Core design decisions

| Decision | Rationale |
| --- | --- |
| `Decimal` everywhere for money/prices, never `float` | Binary floats cannot represent $0.01; fee rounding and profit thresholds must be exact. |
| Prices stored as dollar `Decimal` quantized to 4 dp | Matches Kalshi's current `*_dollars` string format and Polymarket's decimal prices; supports sub-penny ticks. |
| Quantities as `Decimal` quantized to 2 dp | Kalshi now returns fixed-point contract counts (`count_fp`, e.g. `"13.00"`); fractional contracts exist. |
| Adapters return normalized `OrderBook` with **synthetic asks** | Kalshi publishes bids only; a NO bid at $0.40 is a YES ask at $0.60. Normalizing early keeps the opportunity engine venue-agnostic. |
| Fixture-backed adapters are the default | Deterministic tests, no live dependency, no ToS risk in CI. |
| Two-stage matching: machine *candidate* → human *approval* | Title similarity is not settlement equivalence. Only manually approved pairs may be labeled `confirmed`. |
| Rules hashing on approval | Any later rules-text change auto-suspends the pair. |
| Polymarket products are explicit | Global and US have different entities, APIs, identifiers, terms, and settlement rails. |
| USD and USDC remain distinct | Required for legacy Global fixtures; Polymarket US uses USD and must not inherit a depeg haircut. |

## Phases

### Phase 1 — Foundation
Package layout, `pydantic-settings` config, SQLAlchemy 2.x async models, Alembic,
structlog JSON logging with secret redaction, Prometheus metrics, Docker Compose, CI,
Ruff + mypy strict, `.env.example`, security `.gitignore`, threat model.

### Phase 2 — Read-only venue adapters
`VenueAdapter` protocol: `discover_markets()`, `fetch_orderbook()`, `stream()`, `health()`.
Kalshi and Polymarket US REST + WebSocket clients, plus fixture adapters implementing the
same protocol. Reconnect with exponential backoff + jitter, sequence-gap detection with
snapshot resync, freshness/staleness tracking, normalized errors, rate-limit awareness.
The current Global adapter is retained only as explicitly selected legacy code while the US
adapter is built; live mode never substitutes one product for the other.

### Phase 3 — Market matching
Normalizer (entities, thresholds, comparison operators, dates, timezones), candidate
generation (token + fuzzy + structured scoring), structured rules diff across the eight
required dimensions, approval/suspension workflow, rules hashing, review dashboard.

### Phase 4 — Opportunity engine
Depth-walking executable-quantity/VWAP calculation for both directions, venue-specific fee
models (Kalshi ceil-to-centicent + balance-floor rounding; Polymarket `C·rate·p·(1-p)`),
configurable buffers, staleness rejection, dedup + cooldown, explicit rejection reasons,
deterministic and property-based tests.

### Phase 5 — Dashboard and alerts
Server-rendered Jinja2 pages (no heavy frontend): adapter health, markets, suggested pairs,
pair review, live books, opportunities, paper positions, audit log. Console + webhook alerts
with credential redaction.

### Phase 6 — Paper trading
Sequential two-leg simulation with configurable latency, partial fills walked across levels,
book replay, unhedged-exposure tracking, P&L and missed-opportunity reporting, settlement
reconciliation, CSV/JSON export.

### Phase 7 — Guarded execution (NOT IMPLEMENTED)
Interfaces and safety controls only. `DisabledExecutionAdapter` raises on every submit path.
Requires explicit user request after reviewing scanner and paper-trading results.

## Verification gates

Each phase must pass `ruff check`, `ruff format --check`, `mypy --strict`, and `pytest`
before the next phase begins.
