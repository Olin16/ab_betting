# Supervised live read-only smoke test — APPROVED, READY, NOT EXECUTED

**Status: approved by the operator on 2026-08-24. The runner gap found by the preflight
gate is now closed and regression-tested offline. No request in this document has been
sent.**

> ## Preflight blocker history — recorded 2026-08-24, resolved 2026-08-26
>
> The preflight gate in §7.2 requires that the **derived** request plan match §1 exactly.
> It does not. The plan is derived by `event_arb.candidates.build_request_plan`, which is
> the only request plan in the codebase, and it describes **three** of the five rows in §1:
>
> | §1 row | Endpoint | In derived plan? |
> | --- | --- | --- |
> | 1 | `GET .../trade-api/v2/markets` | yes |
> | 2 | `GET .../v1/markets` | yes |
> | 3 | `GET .../v1/market/slug/{id}` | yes |
> | 4 | `GET .../trade-api/v2/markets/{ticker}/orderbook` | **no** |
> | 5 | `GET .../v1/markets/{id}/book` | **no** |
>
> The derived plan additionally asserts `order books fetched : 0 (candidate research
> never reads depth)` and a worst case of **27** requests, against §1's **31**.
>
> This is a gap in the *runner*, not in the endpoints: both `fetch_orderbook`
> implementations already target exactly the §1 row 4 and row 5 paths, one request per
> market. What is missing is a bounded command that fetches books for a hand-picked pair
> set **and declares them in a derived plan first**. `candidates discover` deliberately
> never reads depth; `scan` and `trial` do read depth but print no derived plan, take
> their market set from `TRIAL__WATCHED_PAIRS` rather than from the shortlist, and re-run
> discovery, which would issue catalogue requests beyond the §1 budget.
>
> Success criteria **3**, **5** and **6** depend on rows 4 and 5, so the first approved
> attempt correctly stopped. `event-arb smoke-test` now derives all five rows, enforces
> them before transmission, caps the worst case at 31, and records each response. The
> metadata-only `candidates discover` command remains unchanged in purpose and still reads
> no books.

The purpose is narrow: confirm that the two public REST surfaces still behave as
`docs/api-assumptions.md` records, under supervision, at the smallest scope that can answer
that question. It is not a trial, it collects no profitability evidence, and it places no
orders.

---

## 1. Endpoints

Every endpoint below is a public, unauthenticated `GET`. No other verb is used. No
authenticated host is contacted.

| # | Method | URL | Params | Headers | Count |
| --- | --- | --- | --- | --- | --- |
| 1 | `GET` | `https://external-api.kalshi.com/trade-api/v2/markets` | `status=open`, `limit=250` | none | 1 |
| 2 | `GET` | `https://gateway.polymarket.us/v1/markets` | `active=true`, `closed=false`, `limit=250` | none | 1 |
| 3 | `GET` | `https://gateway.polymarket.us/v1/market/slug/{id}` | — | none | 0–25 |
| 4 | `GET` | `https://external-api.kalshi.com/trade-api/v2/markets/{ticker}/orderbook` | — | none | 0–2 |
| 5 | `GET` | `https://gateway.polymarket.us/v1/markets/{id}/book` | — | none | 0–2 |

**Hosts deliberately not contacted:**

- `https://api.polymarket.us` — the authenticated Polymarket US API. The read-only client
  has no code path to it.
- `wss://external-api-ws.kalshi.com/...` — Kalshi's WebSocket authenticates its handshake
  even for public channels. `ingestion_mode` stays `rest_polling`.
- `https://external-api.demo.kalshi.co` — demo. Production is the target because the
  question is whether the *production* schema matches.

Headers: no `Authorization`, no signature, no API key. With no credentials configured the
signer is `None` and `_auth_headers()` returns `{}`. The command additionally **refuses to
start** if credentials are present, unless `--authenticated-mode` is passed explicitly.

## 2. Request budget

| Phase | Requests | Notes |
| --- | --- | --- |
| Catalogue discovery | 2 | one bounded page per venue |
| Existence recheck | ≤ 25 | `--recheck-top`; response body not read |
| Order books | ≤ 4 | 2 markets × 2 venues, one snapshot each |
| **Total** | **≤ 31** | over roughly 15 minutes |

**Pagination is not followed.** Each venue returns at most one page; a returned cursor is
counted and reported, never acted on. If either venue returns a cursor, that is a *result*
of the test, not a prompt to fetch more.

Rate: the token bucket is **5 req/s per venue**, well under Kalshi's documented Basic tier
(~20 req/s). At ≤ 31 requests total the ceiling is never approached; the limiter exists so
a bug cannot burst.

## 3. Market scope

**Two pairs at most**, chosen from the shortlist by hand before any book is fetched:

- one Fed-decision pair (both venues list an equivalent contract);
- one weather or CPI pair, as a second settlement style.

Selection happens on catalogue metadata only. Books are fetched for those four markets and
no others.

## 4. Data retained

| Retained | Window | Why |
| --- | --- | --- |
| Market metadata for the ≤ 4 markets in scope | 90 d | the schema evidence itself |
| ≤ 4 order-book snapshots | 72 h | normalization evidence; `TRIAL__SNAPSHOT_RETENTION_HOURS` |
| Audit log entries | unbounded | record of what was run |

**Not retained:** raw payloads beyond the bounded debug buffer (off), credentials or
headers in any form, the full catalogue pages (they stay in memory; only the shortlist is
persisted), any Polymarket Global geoblock caller IP.

Target database: a **dedicated file for this test**, not `data/event_arb.db`. Storing venue
data at all is permitted under Kalshi Developer Agreement §3.1 only to facilitate our own
trading; a bounded shortlist plus a 72-hour snapshot window is that, and nothing is shared
with a third party (webhook alerts stay fail-closed).

## 5. Success and failure criteria

**Success — all of:**

1. Both catalogue endpoints return `200` **with no credentials sent**.
2. Every market parses into `VenueMarket` without a `MarketDataError`.
3. Kalshi order books carry `orderbook_fp` with `*_dollars` arrays (A3).
4. Polymarket US markets carry per-market `feeCoefficient`; `outcomes` is a JSON-encoded
   **string**; `orderPriceMinTickSize` is present and positive; `resolutionSource` is still
   **absent**.
5. Both books normalize to two-sided books with non-empty asks on at least one side.
6. Measured refresh batch ≤ configured `max_book_age_ms` minus clock tolerance.
7. Zero `429`s; zero requests to any authenticated host; zero WebSocket connections.

**Failure — any of:** a non-200 that is not an explicitly handled 404; a parse failure on a
market in scope; any documented field missing or changed shape; a `resolutionSource`
appearing (which would change approval readiness); credentials transmitted; any request
outside the table in §1.

**Any failure stops the test.** The correct response is to update
`docs/api-assumptions.md` and the mappers, not to retry.

## 6. Failure handling

| Condition | Behaviour |
| --- | --- |
| **Malformed market** | `parse_market` raises `MarketDataError`; the catalogue loop logs `*.market.parse_failed` and continues, and `parse_failures` is reported. One bad market does not blind the run — but a non-zero count is a **failure** here. |
| **Malformed book** | The mapper refuses rather than guessing. A float tick or an unknown fee policy raises instead of being coerced. |
| **429** | The client pauses that venue for `Retry-After` (30 s if absent) *before* raising, then the run stops. We do not retry into a limit. |
| **Stale book** | Freshness is evaluated at use. A book past `max_book_age_ms` is unusable and no opportunity is computed from it. A failed refresh marks the book stale rather than leaving a phantom fresh one. |
| **Partial discovery** | If one venue returns markets and the other does not, no pairs are generated and the run is a failure. Candidate research already treats an empty side as "no candidates" rather than pairing against stale memory. |
| **Cursor returned** | Counted, reported, **not followed**. |
| **Timeout / 5xx** | `TransientError`; the run stops. No automatic retry, so a supervised operator decides. |

## 7. Confirming no authenticated or order-submission endpoint is reachable

Before the run:

1. `event-arb doctor` — must print `live trading possible : False` and
   `execution adapter : refuses all order operations (verified)`.
2. The preflight request plan is printed and must match §1 exactly, including
   `credentials : none` and `websockets opened : 0`. The plan is **derived from the
   adapters that will serve the requests**, so it cannot drift from what is sent.
3. Confirm no venue credentials are configured. If any are, the command refuses to run;
   unlike general candidate research, the smoke-test command has no authenticated-mode
   escape hatch.

Run only after choosing a brand-new dedicated path:

```bash
EVENT_ARB_DATA_SOURCE=live \
EVENT_ARB_KALSHI__ENVIRONMENT=prod \
EVENT_ARB_POLYMARKET__PRODUCT=us \
EVENT_ARB_INGESTION_MODE=rest_polling \
event-arb smoke-test --database /tmp/event-arb-schema-smoke.db \
  --yes-i-authorize-live-requests
```

The command requires a new file under the operating system's temporary directory and
refuses `data/event_arb.db`, an existing database file, credentials, the demo environment,
Polymarket Global, WebSocket mode, a plan other than the five rows in §1, or an
endpoint/parameter/header that was not declared in that plan.

Structural, not procedural:

- `DisabledExecutionAdapter` is the only execution adapter that exists. `submit`, `cancel`
  and `get_order` each raise `LiveTradingDisabledError` unconditionally; `trading_enabled=True`
  is logged as ignored and grants nothing.
- No read-only client contains a `post`, `put`, `patch` or `delete` call — verified by
  grep across `src/event_arb/venues/`.
- The Polymarket US client's base URL is the public gateway. There is no code path from
  the scanner to `api.polymarket.us`.

During the run: capture the request log and assert afterwards that every line is a `GET` to
a host in §1.

## 8. What this test does *not* establish

- **No profitability evidence.** No opportunity found here means anything economically;
  the scope is two pairs and one snapshot each.
- **No hedge-rate evidence.** One snapshot per market cannot produce `measured` leg-2
  evidence; nothing here informs the adverse-fill or leg-2-failure assumptions.
- **No terms, KYC, eligibility or production-approval conclusion.** A public endpoint
  answering `200` says nothing about whether we are permitted to run a trial.
