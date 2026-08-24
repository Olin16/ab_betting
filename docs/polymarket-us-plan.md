# Polymarket US Adapter — Implementation Plan

Read-only, fixture-backed, no order submission. Phase 7 stays unimplemented throughout.

> **Polymarket US and Polymarket Global are different venues that share a brand.** Nothing
> in `venues/polymarket/` may be repointed by changing a base URL. See
> `docs/venue-roadmap.md`.

## Ground rules

| Rule | Why |
| --- | --- |
| New package `venues/polymarket_us/`, no shared code with Global | Wallets, token IDs, USDC, Polygon, and the geoblock endpoint have no US equivalent; sharing invites silent reuse of a wrong assumption. |
| Fixtures by default; no live calls in automated tests | Same discipline as every other adapter. Live schema checks stay behind the `live` marker. |
| Every schema claim carries a VERIFIED/ASSUMPTION label | Section C of `docs/api-assumptions.md`. |
| WebSocket treated as authenticated | REST docs say market data needs no key, the market WS docs require API-key headers. The conflict is unresolved, so assume the stricter reading. |
| Public endpoint availability ≠ permission | Participant materials may require conformance testing and written production approval. |

## Phase U1 — Identity and settlement

Before any network code, make the two products distinguishable in storage.

- Add a `product` discriminator to persisted Polymarket rows (see "Venue identity" below).
- `restore_pairs` requires the product to match, so a Global approval can never restore as
  a US approval even if IDs collide. The v2 settlement fingerprint already blocks this via
  `settlement_asset` (USD ≠ USDC); the discriminator makes it explicit rather than
  incidental.
- US markets map to `SettlementAsset.USD`. The USDC haircut is driven by the market's
  settlement asset, so it disappears for US pairs automatically — verify with a test rather
  than assuming.
- `RiskFlag.USD_USDC_BASIS` must **not** be raised for Kalshi + Polymarket US.

**Tests:** fingerprint differs across products for otherwise identical markets; restore
refuses a cross-product match; no USDC allowance and no basis flag on a US pair.

## Phase U2 — Fixtures and mappers

- Record synthetic fixtures shaped to the documented US payloads under
  `tests/fixtures/polymarket_us/`.
- Market mapper: slug, market ID, market **side** identifiers, status, close time,
  tick size, minimum quantity, fee parameters. US uses market IDs/slugs and sides, not
  condition/token IDs — the mapper must not carry any token-ID concept.
- Order-book mapper: full depth, `bids`/`offers` (not `bids`/`asks`), USD prices, into the
  existing normalized two-sided `OrderBook`.
- Malformed payloads raise `MarketDataError`; a book built from nothing is `is_complete=False`.

**Tests:** exact Decimal parsing with no float path; depth ordering (bids descending,
offers ascending); zero-size levels dropped; malformed payloads raise; empty book marked
incomplete; every fixture round-trips through the real mapper.

## Phase U3 — Public REST client

- Base `https://gateway.polymarket.us`.
- Market discovery plus **targeted retrieval by market ID/slug**, matching the
  `discover_markets(market_ids=...)` contract the other adapters already implement.
- Full-depth order-book snapshots.
- Conservative rate limiting, `Retry-After` honoured, normalized error taxonomy.
- Pagination handled or explicitly documented as unhandled.

**Tests:** `httpx.MockTransport` only; 429/5xx/4xx map to the right error types; targeted
retrieval issues one request per named market; discovery never runs unbounded when watched
pairs are configured.

## Phase U4 — Fees, ticks, and quantity constraints

- Read fee coefficients, rebates, and rounding from current venue data or explicit
  configuration — never a module constant baked into the calculation path.
- Populate `tick_size` and `min_quantity_increment`, which the opportunity engine already
  applies when walking depth.
- Unknown or unparseable fee data falls back **conservatively** (overstating fees suppresses
  a marginal opportunity; understating invents one).

**Tests:** boundary rounding; conservative fallback on malformed fee data; tick and
increment constraints actually reach `walk_asks`.

## Phase U5 — Authentication

- API-key header signing behind a small, isolated signer, mirroring `kalshi/auth.py`.
- Keys are read from a path at use time, never held in config objects or logged.
- Signing tested with **throwaway keys generated in-process**; tests assert determinism and
  message construction, never server acceptance.
- No wallet, no Polygon, no private-key-on-chain concepts anywhere in this package.

**Tests:** header construction; no secret reaches a log record (extend the existing
redaction suite); missing/invalid key material raises `AuthenticationError`.

## Phase U6 — Authenticated market WebSocket

- `wss://api.polymarket.us/v1/ws/markets` with API-key headers.
- Reconnect with exponential backoff and full jitter; all books stale across a disconnect.
- Malformed messages emit an **immediately stale** book, matching both existing adapters.
- **Sequence/continuity semantics are UNVERIFIED.** Until a recorded multi-market session
  proves otherwise, assume no usable continuity guarantee: rely on full snapshots plus
  periodic REST resynchronization, and attach a `no_sequence_numbers`-style risk flag.
  If the feed does carry a per-subscription counter, adopt the Kalshi approach — one
  subscription per market, state keyed by subscription id.

**Tests:** reconnect/backoff; stale-on-disconnect; malformed message stales the book;
REST resync repairs state; recorded-session replay once a capture exists.

## Phase U7 — Wiring and approval

- `PolymarketProduct.US` selects the new adapter; `validate_live_market_data()` stops
  failing closed only when the adapter exists.
- Every migrated pair needs a **fresh human approval** against US rules. Global approvals
  are not transferable, and the fingerprint change enforces that.
- Bump `SETTLEMENT_FINGERPRINT_VERSION` if any US-specific field joins the fingerprint,
  which invalidates stored approvals and forces re-review.

**Tests:** end-to-end fixture trial on Kalshi + Polymarket US producing a confirmed
opportunity and a paper trade; a Global approval cannot satisfy a US pair.

---

# Venue identity — analysis, not yet a migration

**Is `Venue.POLYMARKET` safe for both products today?** For *approvals*, yes — verified,
not assumed. With identical market IDs, titles, rules text, sources and close times, the
Global and US fingerprints still differ because `settlement_asset` (USDC vs USD) is part of
the v2 fingerprint, and `restore_pairs` suspends on the mismatch.

**Is it sufficient?** No. Three operational problems remain, none of which touch approval
safety:

1. `venue_markets` is unique on `(venue, venue_market_id)`. Both products write
   `venue="POLYMARKET"`, so the constraint no longer means "one row per market on one
   venue".
2. `Scanner.state.markets`, `watched_markets()` and `refresh_books()` key on `Venue` alone.
   A database holding leftover Global rows would have a US-configured scanner issue US API
   calls for Global market IDs.
3. Metrics, health, and `orderbook_snapshots` collapse both products into one `venue`
   label, so a mixed-history database cannot be read reliably.

## Two options

**Option A — product discriminator (recommended now).** Add `product` to `venue_markets`,
`orderbook_snapshots`, and `MarketPairRow`; extend the uniqueness constraint; require a
product match in `restore_pairs`. Existing rows backfill to `global`.

- *Cost:* one Alembic migration, a handful of repository changes, no domain churn.
- *Keeps:* `ArbDirection` semantics, metric label continuity, all 116 `Venue.POLYMARKET`
  references.

**Option B — split the enum into `POLYMARKET_GLOBAL` / `POLYMARKET_US`.** Cleaner identity,
and the only option that permits running both products at once, since `adapters` is keyed
by `Venue`.

- *Cost:* 46 source and 70 test references; `ArbDirection` values
  (`A_KALSHI_YES_POLY_NO`) assume a single Polymarket; `MarketPairRow` columns are named
  `polymarket_market_id`; venue strings persisted in `venue_markets`,
  `orderbook_snapshots`, and inside JSON snapshot payloads need rewriting; Prometheus
  series break continuity.

**Recommendation:** take Option A during Phase U1. The products are mutually exclusive by
jurisdiction, so nothing needs both adapters live at once — which is Option B's only unique
benefit. Revisit Option B if a venue ever needs two concurrently active products, and do it
as its own change with a data migration, never bundled with adapter work.
