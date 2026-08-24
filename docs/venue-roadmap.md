# Venue Roadmap

> **Venue status.** Polymarket **Global** is legacy and is never a US fallback.
> Polymarket **US** (QCX LLC) is implemented for **public REST, read-only, credential-free**.
> Its WebSocket is on the authenticated API and is **not** implemented. Both venues serve
> market data over unauthenticated REST but authenticate their WebSocket handshake even for
> public channels. No real order submission exists. Research candidates are not arbitrage.
> Public endpoint availability does not resolve terms, KYC, eligibility, retention or
> production-approval questions.


## Current target

`event-arb` remains a two-venue, read-only and paper-trading system. The intended pair is
now **Kalshi + Polymarket US**, not Kalshi + the international Polymarket CLOB.

Polymarket US and Polymarket Global must be treated as different venues even though they
share a brand:

| Property | Polymarket US | Polymarket Global |
| --- | --- | --- |
| Operator | QCX LLC d/b/a Polymarket US | Adventure One QSS, Inc. |
| Regulatory model | CFTC-designated US contract market | International platform |
| Public API | `https://gateway.polymarket.us` | Gamma/CLOB APIs |
| Market WebSocket | `wss://api.polymarket.us/v1/ws/markets` | CLOB market channel |
| Settlement | USD | USDC on Polygon |
| Identity | Market IDs/slugs and market sides | Condition/token IDs |
| Current repository adapter | Not implemented | Implemented but legacy |

Official Polymarket US documentation confirms public market discovery, full books, BBO,
settlement data, authenticated market WebSockets, an authenticated trading API, and Python
and TypeScript SDKs. That establishes technical feasibility; it does **not** grant this
project permission to trade or activate Phase 7.

## Required migration

The existing `venues/polymarket/` implementation targets Global and cannot be repointed by
changing a base URL. A Polymarket US adapter needs its own:

1. fixture corpus based on US API payloads;
2. market and market-side mapper;
3. USD order-book mapper for `bids` and `offers`;
4. authenticated WebSocket client and signing tests;
5. fee model and rounding tests based on the current US fee schedule;
6. sequence/continuity and REST-resynchronization policy;
7. schema-contract tests for documented REST and WebSocket messages;
8. explicit venue/product identity in persistence so Global approvals cannot be restored
   as US approvals;
9. removal of the USD/USDC haircut only for US pairs; and
10. a fresh manual rule review for every migrated pair.

Until that work is complete, selecting Polymarket US in live mode fails closed. Live mode
will never silently fall back to Global.

## API uncertainties to settle before live access

- The public REST documentation says market data requires no key, while the market
  WebSocket documentation requires API-key headers. Treat the WebSocket as authenticated.
- The current retail API and the institutional Exchange API are separate integration
  surfaces. This project should use the retail API unless Polymarket US explicitly directs
  the operator elsewhere.
- The individual participant agreement requires any applicable conformance testing and
  written production approval for API connectivity. Confirm how that clause applies to a
  self-service retail API key before a live trial.
- Eligibility is determined through onboarding and current venue policy; do not assume
  blanket availability in every state.
- Fee coefficients, rebates, tick sizes, minimum quantities, and rounding rules are
  changeable and must be read from current venue data or documented configuration.

## Future research, not current scope

The supplied venue analysis also proposed Novig, ProphetX, Betfair, OddsJam, OpticOdds,
and US sportsbooks. Those may be useful later, but they are not part of the present
acceptance criteria and have not had their APIs, terms, state eligibility, data licenses,
or fee models verified.

In particular:

- Odds/data services are candidate discovery inputs, not necessarily execution venues.
- Sportsbook automation must not use browser automation, CAPTCHA avoidance, credential
  sharing, or any interface the operator is not authorized to automate.
- A sportsbook-to-exchange hedge is still non-atomic and must model stake limits, odds
  movement, account limits, void/push rules, grading differences, and second-leg failure.
- No additional venue and no live execution path should be implemented without an
  explicit scope decision and a separate terms/API review.

## Official references

- Polymarket US API introduction: <https://docs.polymarket.us/api-reference/introduction>
- Markets API: <https://docs.polymarket.us/api-reference/market/overview>
- Market WebSocket: <https://docs.polymarket.us/api-reference/websocket/markets>
- Authentication: <https://docs.polymarket.us/api-reference/authentication>
- Official SDKs: <https://docs.polymarket.us/api-reference/sdks/introduction>
- Fee schedule: <https://docs.polymarket.us/fees>
- Regulatory documents: <https://www.polymarketexchange.com/regulatory.html>
- CFTC DCM record: <https://www.cftc.gov/IndustryOversight/IndustryFilings/TradingOrganizations/49571>
