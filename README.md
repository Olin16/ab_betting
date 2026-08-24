# event-arb

A safety-first scanner for **potential** arbitrage between economically equivalent binary
event contracts on **Kalshi** and **Polymarket**.

> **Venue identity matters:** the implemented Polymarket adapter targets the international
> CLOB, which US operators may not use. The intended US deployment target is the separate
> QCX-operated **Polymarket US** API. Its adapter is not implemented yet, and live mode
> fails closed rather than falling back to Global. See
> [`docs/venue-roadmap.md`](docs/venue-roadmap.md).

> **This build collects market data, detects opportunities, and paper-trades them.
> It cannot place a real order.** Live execution (Phase 7) is not implemented: the only
> execution adapter that exists refuses every submission unconditionally.

## What it does

- Normalizes Kalshi and Polymarket order books into a single two-sided representation.
- Requires a **human** to approve a market pair before it is ever called an arbitrage.
  Everything else is a *research candidate*.
- Walks real order-book depth to compute executable quantity and volume-weighted prices.
  Midpoints are never used for profit calculations.
- Models Kalshi's ceil-to-centicent fee rounding and Polymarket's category-based taker fee.
- Treats USD and USDC as distinct assets with an explicit conversion/depeg haircut for
  legacy Global fixtures; Polymarket US settles in USD and requires a separate adapter.
- Suspends an approved pair automatically if either venue's rules text changes.
- Simulates two-leg execution with latency, partial fills, and unhedged exposure.

## What it deliberately does not do

- No geoblock, KYC, rate-limit, or venue-terms circumvention of any kind.
- No claim that any opportunity is risk-free. Cross-venue execution is **not atomic**;
  see [docs/risk-model.md](docs/risk-model.md).
- No live order submission, no fund transfers, no withdrawals.

## Requirements

- Python 3.12+
- Docker + Docker Compose (optional; only needed for the PostgreSQL path)

## Setup

```bash
uv venv --python 3.13
uv pip install -e ".[dev]" aiosqlite
cp .env.example .env          # placeholders only; never commit a real .env
```

## Running the scanner

Everything below runs entirely on recorded fixtures — no network, no credentials.

```bash
# One-shot scan: prints normalized books, opportunities, and rejection reasons.
.venv/bin/event-arb scan

# Same, plus a simulated two-leg paper trade per confirmed opportunity.
.venv/bin/event-arb paper

# Dashboard at http://127.0.0.1:8000
.venv/bin/event-arb serve
```

`event-arb --help` lists all commands. With Docker:

```bash
docker compose up          # PostgreSQL + the dashboard
```

## Tests, lint, types

```bash
.venv/bin/pytest                       # fixture-backed; never touches a live API
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy
```

Optional read-only live tests are marked and **deselected by default**:

```bash
.venv/bin/pytest -m live               # requires credentials; still places no orders
```

## Documentation

| Document | Contents |
| --- | --- |
| [implementation-plan.md](docs/implementation-plan.md) | Phase plan and design decisions |
| [architecture.md](docs/architecture.md) | Module layout and data flow |
| [api-assumptions.md](docs/api-assumptions.md) | **What is verified vs. assumed about each API** |
| [market-matching.md](docs/market-matching.md) | Why title similarity is not equivalence |
| [risk-model.md](docs/risk-model.md) | Execution risks that survive a "perfect" spread |
| [threat-model.md](docs/threat-model.md) | Credentials, keys, logs, dependencies |
| [operations.md](docs/operations.md) | Running it, retention, alerts, live-trading checklist |

## Security

Never commit `.env`, private keys, or wallet material. Secrets are redacted from logs by a
structlog processor (`src/event_arb/observability/logging.py`), not by call-site discipline.
See [docs/threat-model.md](docs/threat-model.md).
