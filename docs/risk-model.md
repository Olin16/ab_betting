# Risk Model

> **Venue status.** Polymarket **Global** is legacy and is never a US fallback.
> Polymarket **US** (QCX LLC) is implemented for **public REST, read-only, credential-free**.
> Its WebSocket is on the authenticated API and is **not** implemented. Both venues serve
> market data over unauthenticated REST but authenticate their WebSocket handshake even for
> public channels. No real order submission exists. Research candidates are not arbitrage.
> Public endpoint availability does not resolve terms, KYC, eligibility, retention or
> production-approval questions.


> The implemented Polymarket model describes the legacy international CLOB. The intended
> US target is Polymarket US, which settles in USD and requires a separate adapter. Do not
> apply Global USDC, fee, sequence, or geoblock assumptions to the US product. See
> `docs/venue-roadmap.md`.

> **No opportunity produced by this system is risk-free.** Every opportunity record
> carries the `non_atomic_execution` flag, without exception, because that risk cannot be
> engineered away from outside the venues.

A "guaranteed $1.00 payout" is guaranteed only if **all** of the following hold: both legs
fill, at the expected sizes, on contracts that really are settlement-equivalent, and both
venues honour their rules and remain solvent. Each of those is a separate assumption.

## 1. Execution risk — the dominant risk

There is no atomic cross-venue transaction between Kalshi and Polymarket. Orders are sent
sequentially into two independent systems.

| Failure | What actually happens | Modeled where |
| --- | --- | --- |
| **First leg fills, second fails** | A fully unhedged directional position at a price chosen for hedging, not speculation. | `paper/executor.py`, status `LEG2_FAILED` |
| **Partial fills** | Only the overlapping quantity is hedged; the remainder is naked. | `PaperTrade.unhedged_quantity` |
| **Price moves between legs** | Realized spread is worse than detected; a thin edge can invert. | `PaperTrade.slippage_vs_detected` |
| **API timeout, unknown order state** | The order may or may not have filled. Retrying can double the position. | `execution/base.py` `OrderState.UNKNOWN` |
| **Duplicate requests** | Same as above; mitigated only by idempotent client order IDs. | `SimulatedOrder.client_order_id` |
| **Market canceled/suspended mid-execution** | One leg can no longer be established or exited. | `RejectionReason.MARKET_NOT_TRADABLE` |
| **Venue outage** | Exposure cannot be closed at any price for the duration. | health tracking, `venue_up` metric |
| **Stale WebSocket state** | The book being priced never existed; the "spread" is an artifact. | freshness policy, sequence-gap resync |
| **Insufficient balance** | Second leg is rejected on funding, not price — an unhedged position for a fully avoidable reason. | `TradingControls` min-balance check |

The simulator is deliberately pessimistic: it re-reads the book at arrival time, applies a
configurable adverse-selection haircut, and never fills above the detected worst price.

## 2. Settlement-equivalence risk

Two contracts on "the same" event can settle differently. This risk is **not** priced —
it is excluded by requiring human approval, because it is not quantifiable in advance.

- **Inclusive vs exclusive thresholds.** `>= 90` pays at exactly 90; `> 90` does not. At
  the boundary, "both legs win" becomes "both legs lose".
- **Different data sources.** NWS preliminary vs NOAA final can report different values
  for the same day.
- **Revision policy.** If one venue honours a correction and the other does not, a
  revision settles both legs to the same side.
- **Postponement / cancellation.** Resolve-No versus void-and-refund are not the same
  economic outcome.
- **Close-time mismatch.** One venue may keep trading — and keep resolving — after the
  other has closed.
- **Ambiguous outcomes.** 50/50 splits, voids, and resolver discretion do not coincide.

Mitigation: `matching/rules_diff.py` compares these dimensions structurally, surfaces
every difference side by side, and refuses approval of a pair with material differences
unless a reviewer explicitly acknowledges them. `matching/approval.py` records both rules
hashes at approval and auto-suspends the pair if either text changes.

## 3. Currency and settlement-asset risk

Kalshi settles in **USD**, as does **Polymarket US** — that pairing carries no cross-asset
exposure and receives no haircut. Polymarket **Global** settles in **USDC** on Polygon.
Those are different
assets:

- **Depeg risk.** USDC has traded below $1.00 before and can again.
- **Conversion cost and delay.** Getting USDC to USD costs fees and takes time.
- **On-chain settlement delay.** Polygon finality is not instantaneous, and gas must be
  available in the right asset.

Modeled as a configurable `usdc_haircut_pct` charged against the USDC-denominated payout,
plus the permanent `usd_usdc_basis` risk flag. It is never silently assumed to be 1.0000.

## 4. Data-quality risk

- **Stale books.** Judged from *local receipt time*, never the exchange timestamp: a
  venue clock running fast would otherwise make an old book look fresh.
- **Sequence gaps.** Kalshi provides `seq`; any non-consecutive value forces a resync and
  marks the book stale. Missing one delta silently corrupts a book, and a corrupt book
  invents spreads.
- **No sequence numbers on Polymarket.** The market channel carries only a hash, so
  continuity cannot be *proven*. Mitigated by full snapshots plus periodic REST resync,
  and disclosed on every opportunity via `no_sequence_numbers`.
- **Thin books.** A single cancellation can remove the entire apparent edge
  (`thin_book`, `single_level_fill`).

## 5. Model risk

- **Fee schedules change.** Rates and multipliers are configuration, never constants in
  the calculation path. Kalshi's per-order rounding accumulator is modeled explicitly.
- **API schemas change.** See `docs/api-assumptions.md`. Mappers raise `MarketDataError`
  on unexpected shapes rather than producing a half-built book.
- **Unverified assumptions.** Several details — notably Kalshi's exact signing message
  and the fee-schedule multiplier — are recorded as unverified and must be confirmed
  before any live use.

## 6. Counterparty and regulatory risk

Not modeled and not mitigable in software: venue solvency, account restriction, rule
changes, and jurisdictional access. The geoblock check is *enforcement*, not mitigation —
it fails closed and refuses submission; it never attempts to work around a restriction.

## Buffers

Configurable and applied to every calculation:

| Buffer | Default | Purpose |
| --- | --- | --- |
| `slippage_buffer_pct` | 0.5% of cost | Price movement between detection and fill |
| `operational_cost_usd` | $0.10 | Gas, transfers, operational overhead |
| `usdc_haircut_pct` | 0.10% of payout | USDC conversion and depeg |
| `min_net_profit_usd` | $0.50 | Do not chase edges that rounding can erase |
| `min_return_pct` | 0.5% | Capital efficiency floor |
| `max_book_age_ms` | 2000 ms | Staleness ceiling |

These are floors on *disclosed* risk, not a guarantee of profitability.
