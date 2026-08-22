# Venue Terms — Review and Design Implications

Kalshi Developer Agreement read in full on **2026-08-22** from
<https://assets.kalshi.com/Kalshi-Developer-Agreement.pdf> (6 pages). Polymarket's Terms
of Use is **not yet reviewed** — see the gate at the bottom.

This is step 1 of the trial plan because it constrains the design, not just the paperwork.

## Kalshi — the governing clause

> **§3 preamble:** "Use of Kalshi APIs is expressly limited to facilitating a member's own
> trading on the Exchange; all other usages are disallowed and may result in account
> suspension."

Everything below follows from that. The API is licensed for *your own trading*, not for
research, not for data collection, and not for anything a third party consumes.

### §3.1 — storage and sharing

> "Collecting, caching, aggregating, or storing data or content accessed via the API
> except for purposes of facilitating your own trading on Kalshi. You may not share such
> data or content with third parties in any manner without prior written authorization
> from Kalshi."

| Implication | Design response |
| --- | --- |
| Order-book snapshots may be stored **only** to facilitate our own trading | Retention is bounded and justified as operational, not archival. A permanent research corpus is **outside** the carve-out. |
| Market data must not reach third parties | Webhook alerts no longer transmit Kalshi market identifiers, prices, or sizes unless explicitly authorized. |
| The dashboard is data sharing if exposed | Localhost-bound; publishing it to others requires Kalshi's written authorization. |
| CSV/JSON exports | Fine for your own use; sending them to anyone else is prohibited. |

### §3.5 — no monitoring or benchmarking

> "Accessing the API for purposes of monitoring the availability, performance, or
> functionality of any of Kalshi's services or for any other benchmarking or competitive
> purposes."

This project tracks adapter health, reconnects, and book age. That is legitimate **only**
as a data-quality check on our own trading inputs — we must know whether a book is fresh
enough to act on. It must not be operated or presented as Kalshi uptime monitoring.
The `event_arb_venue_up` metric is deliberately documented as *adapter* health, and no
availability statistics should be published or shared.

### §4.1.1 — membership required

> "You must be a Member of Kalshi's Exchange and comply with Kalshi's Rulebook."

API access presupposes a registered, KYC'd member account. This resolves the practical
side of api-assumptions **A4**: whether or not market-data endpoints technically accept
unauthenticated calls, the agreement contemplates use by an identified member. Trial
access should be authenticated.

### §4.3 — best practices

Retries on errors, including rate-limit errors, must follow standard best practice.
The client's exponential backoff with full jitter and `Retry-After` handling satisfies
this. Rate limits are token-bucket, **200 read tokens/second on Basic** with most requests
costing 10 tokens (~20 req/s). The configured default of 5 req/s is deliberately well
under that.

### Other prohibitions (no design impact, recorded for completeness)

§3.2 facilitating others' trading · §3.3 market-integrity abuse (spoofing, wash trades)
· §3.4 disruption/DoS · §3.6 accessing others' data · §3.7 sublicensing the API
· §3.8 transmitting over non-encrypted channels.

Liability is capped at **$50.00 USD** and everything is provided "as is". The venue owes
you nothing if the data is wrong, which is a further argument for the staleness and
sequence-gap defences already in place.

## What changed in the code as a result

1. **Webhook alerts are now compliance-gated.** By default an alert says *that* an
   opportunity exists and links to the local dashboard; it no longer ships venue market
   IDs, prices, VWAPs, or sizes to a third-party endpoint. Full payloads require
   `alerts.allow_third_party_market_data=true`, which the operator must set only with
   Kalshi's written authorization.
2. **Retention is bounded** rather than indefinite, and documented as trading-facilitation.

## Outstanding human gates

- [ ] **Polymarket Terms of Use has not been reviewed.** The page is JavaScript-rendered
      and could not be machine-read. A human must read it before live access, checking
      specifically for automated-access permission, data redistribution limits, and
      jurisdictional restrictions.
- [ ] Confirm your Kalshi membership is in good standing and the Rulebook permits your
      intended automated activity.
- [ ] If you ever want to share output — a dashboard link, an export, an alert to a team
      channel — get **prior written authorization from Kalshi** first. §3.1 has no
      de minimis exception.
