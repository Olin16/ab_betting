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

### A3b. Market fields — VERIFIED against a live payload (2026-08-23)
Read-only `GET /markets?status=open` returned 200 **without credentials**, confirming A4.
Corrections found against the live response:

- **`tick_size_dollars` does not exist.** The price increment comes from
  `price_ranges[].step` alongside `price_level_structure` (observed `deci_cent`, step
  `0.0010`). Defaulting to `0.01` was too coarse and made the depth walker skip valid
  sub-penny levels as off-grid. Fixed.
- **`settlement_source` does not exist.** It stays empty rather than being invented; an
  unnamed settlement source is itself material at approval time.
- `*_dollars` and `*_fp` fields are strings, as assumed.
- `rules_primary` can be **empty** on some markets (observed on an MVE combo market), so
  a rules fingerprint may rest on `rules_secondary` alone. Treat such markets as
  unapprovable until the full rules are obtained elsewhere.
- `GET /markets/{ticker}/orderbook` returned `{"orderbook_fp": {"yes_dollars": [],
  "no_dollars": []}}` unauthenticated, confirming **A2** exactly.

### A3. Market fields — ASSUMPTION (partially verified)
`GET /markets` returns `ticker`, `title`, `event_ticker`, `yes_bid_dollars`, `volume_fp`,
`status`, `close_time`. Exact set of `*_dollars` / `*_fp` suffixed fields is **not fully
verified**; the mapper tolerates missing fields rather than failing hard.

### A4. Auth on market data — RESOLVED (REST public, WebSocket authenticated)
Kalshi serves **series, events, markets and order books over unauthenticated REST**, so a
credential-free read-only trial is supported. The earlier "the docs disagree" reading was
wrong: the reference page lists auth headers because they are *accepted*, not because they
are required for market data.

**This does not extend to the WebSocket.** The Kalshi WebSocket handshake is authenticated
**even for public market-data channels**. Streaming therefore requires credentials on this
venue, and `ingestion_mode` defaults to `rest_polling` for exactly that reason.

Requests are still signed when credentials happen to be configured.

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

## Polymarket US — implemented and VERIFIED against a live payload (2026-08-23)

`GET https://gateway.polymarket.us/v1/markets?active=true&closed=false` returned 200
**without any credential**. Corrections found against the live response:

- **`feeCoefficient` is published per market** (observed `0.06`). This satisfies the
  per-market fee requirement directly and is now preferred over any configured default.
- **`outcomes` arrives as a JSON-encoded string** (`'["Yes","No"]'`), not a list; reading
  it undecoded would iterate characters. `marketSides` carries the authoritative side
  identity (`identifier`, `description`, `long`).
- **`orderPriceMinTickSize` is `0.001`**, finer than the assumed penny.
- **`resolutionSource` does not exist.** No named settlement source is published.
- `status` is an enum string (`MARKET_STATUS_OPEN`) alongside the boolean flags.
- `bestBidQuote`/`bestAskQuote` are objects carrying `currency: "USD"`, confirming USD
  settlement at the wire level. No `condition_id`, `token_id`, `polygon` or `usdc` appears
  anywhere in the payload — the products really are separate.

**The `description` question is answered: it does carry enforceable rules.** Real
descriptions state postponement, rescheduling, cancellation and elimination behaviour.
Two caveats for approval: settlement language is partly discretionary ("*may* settle at
last fair market prices"), and no authoritative settlement source is named.

## Polymarket US — implementation notes

Checked against official documentation on **2026-08-23**. These facts define the next
fixture-backed adapter; they are not evidence that the current Global mapper is compatible.

### US1. Product and settlement — VERIFIED

Polymarket US is operated by QCX LLC d/b/a Polymarket US, a CFTC-designated contract
market. It is distinct from the international product and trades in **USD**, not USDC on
Polygon.

Sources: <https://docs.polymarket.us/getting-started/what-is-polymarket-us>,
<https://www.cftc.gov/IndustryOversight/IndustryFilings/TradingOrganizations/49571>

### US2. Public market REST — VERIFIED paths, ASSUMPTION full schema

Public base: `https://gateway.polymarket.us`.

- `GET /v1/markets`
- `GET /v1/market/id/{id}`
- `GET /v1/market/slug/{slug}`
- `GET /v1/markets/{slug}/book`
- `GET /v1/markets/{slug}/bbo`
- `GET /v1/markets/{slug}/settlement`

The documented full book contains `marketData.bids` and `marketData.offers`; each level
has a USD `px.value` and string `qty`. Exact live types, pagination, timestamps, tick size,
minimum quantity, and rule-text fields must be captured in fixtures before implementation.

Source: <https://docs.polymarket.us/api-reference/market/overview>

### US3. Market WebSocket — VERIFIED path, ASSUMPTION continuity

`wss://api.polymarket.us/v1/ws/markets` supports full market-data, lite/BBO, and trade
subscriptions by market slug. The WebSocket documentation requires API-key headers using
`timestamp + "GET" + path` signing, even though public REST market data requires no key.
Treat the WebSocket as authenticated and fail closed without credentials.

The documentation says messages are delivered in order but the reviewed examples do not
establish a sequence field, snapshot/delta semantics, or a formal gap-recovery protocol.
Use full-book snapshots plus periodic REST resynchronization until recorded traffic proves
stronger continuity guarantees.

Sources: <https://docs.polymarket.us/api-reference/websocket/overview>,
<https://docs.polymarket.us/api-reference/websocket/markets>

### US4. Fees — VERIFIED published formula, changeable parameters

The published US fee formula is `fee = theta × contracts × p × (1-p)`. The schedule
reviewed on 2026-08-23 lists a taker coefficient and maker rebate, but temporary rebate
programs and effective dates make those values unsafe to hard-code. Query or configure the
current market/account fee treatment and verify rounding before using live profitability.

Source: <https://docs.polymarket.us/fees>

### US5. Authentication and permission — HUMAN GATE

Official retail documentation describes API keys after app onboarding and identity
verification. Participant materials also require applicable conformance procedures and
written approval before production API connectivity. Confirm the operator's exact access
path with Polymarket US; public documentation is not a substitute for contractual
permission.

Sources: <https://docs.polymarket.us/api-reference/authentication>,
<https://www.polymarketexchange.com/regulatory.html>

### US6. Official SDK — VERIFIED availability, not yet selected

The official `polymarket-us` Python SDK advertises async support, typed REST resources,
authentication, and WebSocket support. Evaluate and pin it only after reviewing its source,
license, transitive dependencies, Decimal behavior, and secret-handling boundaries.

Source: <https://docs.polymarket.us/api-reference/sdks/introduction>

---

## Polymarket Global — legacy adapter

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
Polymarket Global settles in **USDC** on Polygon; Kalshi settles in **USD**. These are modeled as
distinct assets. Depeg and conversion cost are exposed as explicit risk flags and a
configurable haircut, never assumed to be 1.0000 silently.

---

## Sportsbook odds providers (OpticOdds / OddsJam)

### D0. Provider identity — VERIFIED
`https://developer.oddsjam.com/` returns **301** to `https://developer.opticodds.com/`
(observed 2026-08-23). They are one API. Both provider identities are retained in the data
model for provenance; the schema family is shared.

### D1. Licensing — VERIFIED as a blocker
API access requires a license key obtained through sales contact. No self-serve tier, no
published pricing, no free trial. The public Terms of Service state "You may not use the
Service for any revenue-generating endeavor or commercial enterprise", which is a general
user agreement rather than the API license. **A retail dashboard subscription does not
convey API rights.** No live provider client may be built until a written agreement exists.

### D2. REST surface — VERIFIED (documentation), UNTESTED (no calls made)
Base `https://api.opticodds.com/api/v3/`. Auth via `X-Api-Key` header; a `key` query
parameter is also documented but is **deliberately not used** because query strings leak
into logs and intermediaries.

`GET /fixtures/odds` requires `sportsbook` (1–5 values) plus one of `fixture_id`,
`team_id`, `player_id`. `odds_format` defaults to `AMERICAN`.

Quote fields: `sportsbook`, `market`, `market_id`, `selection`, `normalized_selection`,
`price`, `points` (nullable), `selection_line` (nullable), `is_main`, `timestamp`,
`limits.max` (**nullable**), `player_id`, `team_id`, `grouping_key`, `deep_link`.

- **CRITICAL:** `price` is a JSON *number*. A stock decoder yields a binary float before
  our code sees it. Payloads must be decoded with `json.loads(..., parse_float=Decimal)`.
- **ASSUMPTION:** `timestamp` is a Unix epoch float in seconds. Not stated explicitly.
- **ASSUMPTION:** `limits.max` is a currency amount in the account's settlement currency,
  and its absence means "no published limit", not "unlimited".

### D3. Streaming — VERIFIED (documentation), UNTESTED
`GET /stream/odds/{sport}` via Server-Sent Events. Events: `connected`, `ping` (every 5 s),
`odds`, `locked-odds`, `fixture-status`. Continuity is by `entry_id`; reconnect supplying
`last_entry_id`. Documented `retry: 5000`.

- **ASSUMPTION:** `last_entry_id` replays every missed event rather than only the latest
  per selection. Until confirmed against a recorded session, a reconnect is treated as
  invalidating local state, exactly as with the exchange feeds.
- **ASSUMPTION:** `locked-odds` means suspended *or* removed; the two are not
  distinguished, so both map to a suspended quote rather than a deletion.

### D4. Rate limits — VERIFIED
2500 requests / 15 s general; 250 / 15 s for new streaming connections; 10 / 15 s for
historical odds.

### D5. Retention and redistribution — UNVERIFIED
Not addressed in public documentation. Must be settled in the written license before live
use. Bounded retention is applied by default.

### D6. Other platforms — UNVERIFIED
Claims that Novig or ProphetX expose usable retail APIs come from third-party summaries
only. `docs.prophetx.co` and `docs.novig.com` appear to exist, but neither has been
confirmed as retail-accessible under terms this operator can satisfy. Recorded as
unverified; no implementation.

---

## Cross-cutting

### C0. Venue terms — REVIEWED for Kalshi
The Kalshi Developer Agreement was read in full on 2026-08-22. It materially constrains
storage and sharing: API use is limited to facilitating your own trading, and data may not
be shared with third parties without written authorization. See **docs/compliance.md**.
Polymarket Global's Terms of Use was read in full on 2026-08-23. It prohibits trading by US
persons and other listed territories with no exceptions, bars Restricted Persons from
accessing the Technology Features (the CLOB API) at all, and bars API access to its Data by
non-retail proprietary trading entities absent written agreement. **This may make the
Polymarket leg unusable regardless of implementation.**

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
