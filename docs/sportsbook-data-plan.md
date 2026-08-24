# Sportsbook Odds Data — Plan and Findings

Read-only ingestion of licensed sportsbook-odds data. **No bet placement, ever.** This
document is Phase S0 of that work: what was verified, what is licensed, and what the design
must not assume.

## The headline findings

**1. OddsJam and OpticOdds are one API, not two providers.**
`https://developer.oddsjam.com/` returns **301 Moved Permanently** to
`https://developer.opticodds.com/` (observed 2026-08-23). OpticOdds is the B2B/API arm;
OddsJam is the retail-facing product. Treating them as independent integrations would be
modelling a distinction that does not exist at the API layer.

The code still carries **both provider identities** so that data provenance is preserved
and a future divergence is representable — but they share a schema family, and that
sharing is documented rather than accidental.

**2. API access is enterprise-licensed and is not part of a retail subscription.**
A license key is obtained by sales contact; there is no self-serve tier, no published
pricing, and no free trial. The public Terms of Service state *"You may not use the Service
for any revenue-generating endeavor or commercial enterprise"* — that is a general user
agreement, not the API license, which reinforces that programmatic use sits under a
**separate written agreement**.

> **Consequence: Phase S3 (a live read-only provider client) is BLOCKED.** No license has
> been verified for this operator, so this work stops at fixture-backed adapters. That is a
> licensing decision, not a technical one. Public endpoint availability does not grant
> contractual permission.

## Verified API surface (documentation only — no calls made)

Source: <https://developer.opticodds.com>, read 2026-08-23.

| Item | Value |
| --- | --- |
| Base URL | `https://api.opticodds.com/api/v3/` |
| Auth | `X-Api-Key` header (a `key` query parameter also works — **do not use it**, query strings leak into logs and proxies) |
| Odds | `GET /fixtures/odds` — requires `sportsbook` (1–5) plus one of `fixture_id`, `team_id`, `player_id` |
| Odds formats | `AMERICAN` (default), `DECIMAL`, `PROBABILITY`, `MALAY`, `HONG_KONG`, `INDONESIAN` |
| Streaming | `GET /stream/odds/{sport}` (Server-Sent Events) |
| Stream events | `connected`, `ping` (5 s), `odds`, `locked-odds`, `fixture-status` |
| Continuity | `entry_id` on each event; reconnect with `last_entry_id=<id>` |
| Rate limits | 2500 req / 15 s general · 250 / 15 s new stream connections · 10 / 15 s historical odds |

Quote schema fields that matter to this project:

```
sportsbook · market · market_id · selection · normalized_selection · name
price · points (nullable) · selection_line (nullable) · is_main · timestamp
limits: { max: number | null }      <-- the stake limit, and it is nullable
player_id · team_id · grouping_key · deep_link
```

### Two schema details with real consequences

**`price` arrives as a JSON number.** Decoding with a stock JSON parser produces a binary
float before any of our code sees it, which would defeat the Decimal discipline at the
point of ingestion. Provider payloads must be decoded with `json.loads(..., parse_float=Decimal)`,
never `response.json()`.

**`limits.max` is nullable.** This is exactly the case the design must handle honestly: a
quote without a stake limit is not executable depth. Missing limits are represented
explicitly and raise a liquidity risk flag rather than being defaulted to some assumed size.

## What a sportsbook quote is *not*

A sportsbook quote is a **price a book is currently willing to offer**, not a resting order
with published depth. Forcing it into the exchange `OrderBook` abstraction would import
three assumptions that are false:

| Exchange order book | Sportsbook quote |
| --- | --- |
| Depth at multiple price levels | One price, and a limit only if the provider supplies it |
| Anonymous, first-come execution | Book may refuse, limit, or void your action |
| Price is the trade | Price may move between seeing it and acting on it |

So sportsbook data gets its own provider-neutral models (`SportsbookEvent`,
`SportsbookMarket`, `SportsbookQuote`) and its own adapter interface (`OddsFeedAdapter`).
They never masquerade as `OrderBook`.

## Matching: why name similarity is even weaker here than on exchanges

Cross-venue exchange matching already requires human approval. Sportsbook matching is
harder, because two quotes can name the same teams and the same number and still settle
differently:

| Dimension | Why it decides money |
| --- | --- |
| Participant identity | Team/player naming differs per book; reserves and youth sides collide |
| Home/away orientation | An inverted spread is a bet on the other side |
| Scheduled start | Doubleheaders and rescheduled fixtures share names |
| League | Same club names across competitions |
| Market type / period | Full game vs 1H vs 3-way moneyline are different contracts |
| Line / threshold | −3.5 and −3 differ by exactly the push case |
| **Overtime inclusion** | Books differ on whether OT counts; exchanges often specify |
| **Push behaviour** | Stake returned vs graded loss |
| Void / cancellation | Void-and-refund is not a loss |
| Postponement | Some books void after N hours, exchanges may resolve No |
| Abandoned events | Partial-game grading rules diverge sharply |
| Stat corrections | Book grades at final whistle; exchange may honour revisions |
| Settlement source | Official league feed vs data provider |
| Jurisdictional availability | A quote you cannot legally take is not an opportunity |

Every sportsbook↔exchange mapping is a **research candidate** until a human approves it,
using the same approval and settlement-fingerprint machinery as exchange pairs.

## Compliance posture

- **No scraping.** No browser automation, no authenticated-dashboard access, no CAPTCHA or
  anti-bot handling. Ever. Data comes from a licensed API or from fixtures.
- **No bet placement.** There is no submission path and none will be added; sportsbook
  execution stays manual and out of scope.
- **Retention and redistribution are unverified** for this provider and must be settled in
  the written license before any live use. Until then the bounded retention already applied
  to exchange data is the default posture.
- **Third-party claims are not evidence.** Reports that Novig or ProphetX expose a usable
  retail API are recorded as **unverified**; `docs.prophetx.co` and `docs.novig.com` appear
  to exist but neither has been confirmed as offering retail API access under terms this
  operator can satisfy. They remain roadmap items with no implementation.

## Phases

| Phase | Scope | Status |
| --- | --- | --- |
| S0 | Investigation, licensing, design | **Done** — this document |
| S1 | Provider-neutral models, `OddsFeedAdapter`, odds math, persistence | Implemented |
| S2 | Fixture-backed OddsJam and OpticOdds adapters | Implemented |
| S3 | Live read-only provider client | **BLOCKED — no verified license** |
| S4 | Research-candidate sportsbook↔exchange comparison (paper only) | Implemented |

Phase S3 stays blocked until a written API agreement exists. When it does, the fixture
adapters already exercise the real mappers, so the live client is a transport swap rather
than a rewrite.
