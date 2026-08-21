# Architecture

## Data flow

```
  Kalshi REST/WS ─┐                        ┌─ normalized OrderBook ─┐
                  ├─ VenueAdapter ─ mapper ┤                        ├─ BookStore ─┐
  Polymarket ─────┘   (or FixtureAdapter)  └─ VenueMarket ──────────┘   (freshness)│
                                                     │                             │
                                                     ▼                             ▼
                                            candidate generation            OpportunityService
                                                     │                             │
                                                     ▼                    both directions, depth
                                            rules diff (8 dimensions)      fees, buffers, flags
                                                     │                             │
                                                     ▼                             ▼
                                          HUMAN APPROVAL  ──────────────▶  confirmed / research
                                          (hashes recorded)                        │
                                                                                   ▼
                                                                         PaperExecutor (2 legs)
                                                                                   │
                                                                                   ▼
                                                                    PaperPortfolio / reports
```

Live execution is **absent** from this diagram because it is absent from the build.

## Layer responsibilities

| Layer | Module | Responsibility |
| --- | --- | --- |
| Money | `domain/money.py` | Decimal grids; rejects `float` outright |
| Domain | `domain/models.py`, `enums.py` | Frozen value objects, no I/O |
| Venues | `venues/` | Protocol + Kalshi/Polymarket/Fixture adapters, mappers, fees, auth |
| Books | `orderbooks/` | Latest-book store, freshness policy, stream synchronization |
| Matching | `matching/` | Normalization, candidate scoring, rules diff, approval lifecycle |
| Opportunities | `opportunities/` | Depth walking, profit calculation, risk flags, scan cycle |
| Paper | `paper/` | Two-leg simulation, exposure, portfolio, settlement |
| Execution | `execution/` | Interfaces and controls only — **disabled** |
| Persistence | `persistence/` | SQLAlchemy 2.x models, exact-decimal storage |
| API | `api/` | FastAPI + Jinja2 dashboard, JSON endpoints |
| Observability | `observability/` | Redacting JSON logs, Prometheus metrics, health |

## Key invariants

1. **No `float` in the money path.** `dec()` raises `TypeError` on a float.
2. **Kalshi asks are synthesized once, at the mapper.** Downstream code is venue-agnostic.
3. **Freshness is measured from local receipt time**, never the exchange timestamp.
4. **Only `pair.is_tradable_pair` produces `OpportunityLabel.CONFIRMED`.** The label is
   derived from the pair, not passed in.
5. **Every opportunity carries `non_atomic_execution`.**
6. **Rules integrity is checked before pricing** in every scan cycle.
7. **Read-only adapters have no order-entry surface.** Enforced by test.

## Concurrency

`asyncio` throughout. `BookSynchronizer` runs one pump task per adapter plus a periodic
REST resync for Polymarket (which cannot prove stream continuity). WebSocket streams
reconnect with exponential backoff and **full jitter**; every book is marked stale across
a disconnect, since correctness cannot be assumed across a gap.

## Why these choices

**Server-rendered dashboard.** The data is tabular and refreshed on demand. A SPA would
add a build toolchain and an attack surface for no benefit.

**Decimal-as-text in the database.** SQLite has no exact decimal type and round-trips
`NUMERIC` through binary float, which would defeat the entire fixed-point discipline. Text
is exact on both SQLite and PostgreSQL, and no SQL-side arithmetic is performed on money.

**Fixture adapters implement the real interface and use the real mappers.** Tests exercise
actual Kalshi/Polymarket parsing code, so a schema-handling bug fails in CI rather than in
production.

**Separate `execution` package with no working implementation.** Trading capability is a
distinct component that must be deliberately built, not a flag that can be flipped.
