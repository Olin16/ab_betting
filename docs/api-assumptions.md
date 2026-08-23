# API Assumptions Requiring Verification

Checked against official documentation on **2026-08-21**. Treat every endpoint and schema as
changeable. Each item below is either **VERIFIED** (read from official docs on that date) or
**ASSUMPTION** (needs confirmation before any live use).

---

## Kalshi

### A1. Environments — VERIFIED
Source: <https://docs.kalshi.com/getting_started/api_environments>

- Production REST (recommended): `https://external-api.kalshi.com/trade-api/v2`
- Production REST (also supported): `https://api.elections.kalshi.com/trade-api/v2`
- Production WS (recommended): `wss://external-api-ws.kalshi.com/trade-api/ws/v2`
- Demo REST: `https://external-api.demo.kalshi.co/trade-api/v2`
- Demo WS: `wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2`

### A2. Orderbook shape — VERIFIED, and recently changed
Source: <https://docs.kalshi.com/getting_started/orderbook_responses>,
<https://docs.kalshi.com/api-reference/market/get-market-orderbook>

```json
{"orderbook_fp": {"yes_dollars": [["0.1500", "100.00"]], "no_dollars": [["0.1500", "100.00"]]}}
```

- **Bids only.** No asks are returned. A YES bid at $0.60 is a NO ask at $0.40.
- Prices are **dollar strings** (`"0.4200"`), not integer cents. Sub-penny pricing is supported.
- Quantities are **fixed-point strings** (`"13.00"`), i.e. fractional contracts are possible.
- Arrays are sorted **ascending**; the best (highest) bid is the **last** element.
- `GET /markets/{ticker}/orderbook`, optional `depth` (0–100; 0 = all levels).

> **Migration risk:** older integer-cent `orderbook.yes` fields appear in much third-party code
> and older docs. This project targets the `orderbook_fp` / `*_dollars` form only.
> **ASSUMPTION:** field names remain `orderbook_fp.yes_dollars` / `no_dollars`.

### A3. Market fields — ASSUMPTION (partially verified)
`GET /markets` returns `ticker`, `title`, `event_ticker`, `yes_bid_dollars`, `volume_fp`,
`status`, `close_time`. Exact set of `*_dollars` / `*_fp` suffixed fields is **not fully
verified**; the mapper tolerates missing fields rather than failing hard.

### A4. Auth on market data — CONFLICTING, needs verification
The Get Market Orderbook reference lists `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-SIGNATURE`,
`KALSHI-ACCESS-TIMESTAMP` headers, while the market-data quick start states market-data
endpoints "require no authentication headers".
**Resolution used:** sign requests whenever credentials are configured, and fall back to
unauthenticated requests when they are not. Verify before relying on unauthenticated access.

### A5. Request signing — ASSUMPTION
RSA-PSS (SHA-256, salt length = digest length) over the string
`timestamp_ms + HTTP_METHOD + path`, base64-encoded, with the API key ID in
`KALSHI-ACCESS-KEY`. Whether `path` includes the `/trade-api/v2` prefix and whether the query
string is included is **not confirmed** by the pages read. Signing is unit-tested against a
locally generated throwaway key for *determinism*, not for server acceptance.

### A6. WebSocket protocol — VERIFIED
Source: <https://docs.kalshi.com/websockets/orderbook-updates>

Envelope: `{"type", "sid", "seq", "msg"}`. Subscribe: `{"id", "cmd": "subscribe",
"params": {"channels": ["orderbook_delta"], "market_tickers": [...]}}`.

- `orderbook_snapshot.msg`: `market_ticker`, `market_id`, `yes_dollars_fp`, `no_dollars_fp`
- `orderbook_delta.msg`: `market_ticker`, `market_id`, `price_dollars`, `delta_fp`, `side`, `ts_ms`
- Gap recovery: `update_subscription` with the `get_snapshot` action returns a fresh
  `orderbook_snapshot` without modifying the subscription.
- **RESOLVED BY DESIGN:** whether `seq` advances per market or per subscription is
  ambiguous when one subscription names several markets, and guessing either way is
  unsafe — a subscription-wide counter would look like a gap on every interleaved update,
  while the opposite assumption would silently miss real gaps. The implementation
  therefore issues **one subscription command per market**, multiplexed over a single
  connection, and tracks `seq` against the assigned `sid`. A `sid` then names exactly one
  market and the counter is unambiguous under either interpretation. Verify the actual
  semantics during the supervised smoke test; the design is correct regardless.
- **ASSUMPTION:** multiple `subscribe` commands are accepted on one connection. If Kalshi
  rejects this, fall back to one connection per market.
- `use_yes_price: true` is now sent explicitly rather than relying on the default, which
  Kalshi has signalled may change; a silent flip would invert NO-side prices.

### A7. Fees — VERIFIED formula, ASSUMPTION on parameters
Sources: <https://docs.kalshi.com/getting_started/fee_rounding>, Kalshi fee schedule (July 2026).

General form: `fee = ceil(M × 0.07 × C × P × (1 − P))`, C = contracts, P = price in dollars.

Rounding, per the fee-rounding page:
1. Round the trade fee **up to the nearest $0.0001** (a "centicent").
2. `balance_change = revenue − trade_fee`.
3. **Floor** `balance_change` toward −∞ to the member's balance precision
   (direct members $0.0001; non-direct members $0.01).
4. `rounding_fee = balance_change − floor(balance_change)`.
5. A per-**order** accumulator sums rounding fees across fills; once it exceeds $0.01 a
   whole-cent rebate is issued. Net fee = trade fee + rounding fee − rebate, never < 0.

- **ASSUMPTION:** the fee *rate* used in the documentation's own worked examples could
  not be reconstructed from the published numbers (a row showing revenue -$0.055 with a
  $0.0085 trade fee does not correspond to a 0.07 coefficient). The rounding *procedure*
  above is verified and is tested directly against a published row; the rate and
  multiplier are configuration-only and must be taken from the live fee schedule.
- **ASSUMPTION:** the multiplier `M` is 1.0 for standard markets. Some series/products carry
  different multipliers and maker fees. The code reads a per-market `FeeConfig`; it does not
  hard-code a single rate. Values must be refreshed from the live fee schedule before use.
- **ASSUMPTION:** non-direct member precision ($0.01) is the correct default.

---

## Polymarket

### B1. Geoblock — VERIFIED
Source: <https://docs.polymarket.com/api-reference/geoblock>

`GET https://polymarket.com/api/geoblock` → `{"blocked": bool, "ip": str, "country": str,
"region": str}`. This project **fails closed**: any non-`false` value, any error, any timeout,
or any unparseable body blocks order submission.

### B2. CLOB REST — VERIFIED (paths), ASSUMPTION (full schema)
Base: `https://clob.polymarket.com`. `GET /book?token_id=...` returns `bids`/`asks` with
`price` and `size` string fields, plus tick size. `POST /books` batches up to 500 tokens.
Market discovery via the Gamma API (`https://gamma-api.polymarket.com`) is an **ASSUMPTION**;
field names for market metadata are not verified here.

### B3. WebSocket — VERIFIED
Source: <https://docs.polymarket.com/api-reference/wss/market>

- URL: `wss://ws-subscriptions-clob.polymarket.com/ws/market`
- Subscribe: `{"assets_ids": [...], "type": "market", "initial_dump": true, "level": 2}`
- Events: `book` (`asset_id`, `market`, `bids[]`, `asks[]`, `timestamp`, `hash`),
  `price_change` (`price_changes[]` each with `asset_id`, `price`, `size`, `side`, `hash`),
  `last_trade_price`, `tick_size_change` (`old_tick_size`, `new_tick_size`).
- Timestamps are Unix **milliseconds**. Send `"PING"` every 10 s.
- **ASSUMPTION:** there is no monotonic sequence number on this channel — only a `hash`.
  Gap detection therefore relies on the hash changing plus periodic REST resync, which is
  weaker than Kalshi's `seq`. This is recorded as a risk flag on every Polymarket book.

### B4. Fees — VERIFIED formula and market-level precedence
Source: <https://docs.polymarket.com/trading/fees>

`fee = C × feeRate × p × (1 − p)`. Takers only; **makers are never charged**.
Rates are **category-dependent**: crypto 0.07; sports/economics/culture/weather/general
0.05; finance/politics/mentions/tech 0.04; geopolitical/world events 0.00. Fees round
to 5 decimal places, minimum charge 0.00001 USDC.

- Market metadata can expose `feesEnabled` and `feeSchedule.rate`. The mapper gives those
  per-market values precedence over category defaults. `feesEnabled: false` forces zero;
  a valid `feeSchedule.rate` is used verbatim; category mapping is a fallback; an unknown
  category uses the conservative maximum published rate.
- **ASSUMPTION:** the field names and units of `feesEnabled` and `feeSchedule.rate` remain
  current. Optional live schema tests should fail if the venue changes them.
- **ASSUMPTION:** `p` in the formula is the execution price of the share being bought.
- Gas/relayer costs are not documented as Polymarket fees; they are modeled separately as an
  operational-cost buffer.

### B5. Settlement asset — VERIFIED (material to arbitrage)
Polymarket settles in **USDC** on Polygon; Kalshi settles in **USD**. These are modeled as
distinct assets. Depeg and conversion cost are exposed as explicit risk flags and a
configurable haircut, never assumed to be 1.0000 silently.

---

## Cross-cutting

### C0. Venue terms — REVIEWED for Kalshi
The Kalshi Developer Agreement was read in full on 2026-08-22. It materially constrains
storage and sharing: API use is limited to facilitating your own trading, and data may not
be shared with third parties without written authorization. See **docs/compliance.md**.
Polymarket's Terms of Use is still unreviewed (JavaScript-rendered; needs a human).

### C0b. Approval fingerprint — decision
An approval is verified against a **versioned settlement fingerprint**
(`SETTLEMENT_FINGERPRINT_VERSION`), not just the rules prose. It covers title, rules text,
settlement source, close time (as an absolute instant, so a timezone change is visible),
the outcome set, and the settlement asset. Hashing prose alone let a venue move the cutoff
or add an outcome without invalidating an approval. Bumping the version invalidates every
stored approval and forces re-review, which is the intended behaviour.

Approvals are additionally only restored when **both legs were retrieved from the venue
during the current run**. Cached metadata matches by construction the hash it was stored
with, so it can never falsify an approval.

### C1. Data retention — decision, not assumption
Only normalized market metadata, order-book snapshots/deltas, and derived analytics are
persisted, for operating and auditing this scanner. Raw API payloads are retained only in a
bounded debug buffer (default off). No redistribution. See `docs/operations.md`.
The Kalshi Developer Agreement has now been read (docs/compliance.md): §3.1 permits storage
only "for purposes of facilitating your own trading on Kalshi" and forbids sharing with
third parties absent written authorization. Retention is therefore bounded and operational,
not archival. Polymarket's terms still require human review.

### C2. Rate limits — VERIFIED for Kalshi, ASSUMPTION for Polymarket
Kalshi uses a token bucket, not a request rate: **200 read tokens/second on Basic**, most
requests costing 10 tokens (~20 req/s), with burst up to twice the per-second budget.
Higher tiers (Advanced 300, Expert 600, ... Prestige 10,000) are earned by volume or
granted manually. The configured default of 5 req/s is deliberately well under Basic.
Polymarket's limits remain unverified; the same conservative default applies.
Both clients honor `Retry-After` on 429, as §4.3 of the Developer Agreement requires.
