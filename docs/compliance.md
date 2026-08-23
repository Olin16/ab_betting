# Venue Terms — Review and Design Implications

Kalshi Developer Agreement read in full on **2026-08-22** (6 pages). Polymarket Terms of
Use read in full on **2026-08-23** (19 pages, operator-supplied PDF).

This is step 1 of the trial plan because it constrains the design, not just the paperwork.

> ## ⚠ Read this first
>
> **Polymarket prohibits trading by anyone in the United States, with no exceptions, and
> bars Restricted Persons from accessing its Technology Features at all — which includes
> the CLOB API this project reads.** If the operator is US-based, the cross-venue thesis
> is not merely blocked at the order-submission step; the read-only data collection is
> also outside the Terms. See "Polymarket — jurisdiction" below. This is a legal question
> about who is operating the system, not a technical one, and no amount of engineering
> resolves it.

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

---

## Polymarket — jurisdiction (decisive)

> "USE OF THE SITE, PLATFORM OR TECHNOLOGY FEATURES FOR TRADING IS NOT PERMITTED BY
> PERSONS OR ENTITIES WHO RESIDE IN, ARE LOCATED IN, ARE INCORPORATED IN, HAVE A
> REGISTERED OFFICE IN, OR HAVE THEIR PRINCIPAL PLACE OF BUSINESS IN THE UNITED STATES OF
> AMERICA, ALBERTA, AUSTRALIA, BELGIUM, BRITISH COLUMBIA, FRANCE, GERMANY, HUNGARY,
> IRELAND, ITALY, ONTARIO, POLAND, QUEBEC, RUSSIA, SINGAPORE, SLOVAKIA, SLOVENIA, TAIWAN,
> THAILAND, THE NETHERLANDS, NEW ZEALAND, THE UNITED KINGDOM, OR ANY OTHER RESTRICTED
> TERRITORY ... **THERE ARE NO EXCEPTIONS**"

The restriction extends to anyone acting **on behalf of**, or **directed, coordinated or
controlled by**, a Restricted Person.

Crucially, the bar is wider than trading. The Terms split the offering into **Content
Features** (news and information on the Site) and **Technology Features** (the Protocol
and Platform reached by connecting a wallet — the CLOB and its API). And:

> "If you are in a Restricted Jurisdiction ... you are only permitted to use the Content
> Features ... and may not use the Site or any other Interface for any other purpose and
> you may not access the Technology Features"

So a Restricted Person may read Polymarket's public news pages and nothing else. The
order-book API this project consumes is a Technology Feature.

> "USE OF A VIRTUAL PRIVATE NETWORK ("VPN") OR ANY SIMILAR TOOL TO ATTEMPT TO OR TO
> CIRCUMVENT THE RESTRICTIONS SET FORTH HEREIN IS STRICTLY PROHIBITED."

This project contains no proxy, VPN, or IP-rotation capability and will not acquire one.
The geoblock checker calls the venue's own endpoint and fails closed; that is enforcement
of the restriction, never a way around it.

**Consequence:** if the operator is in any listed jurisdiction, this system must not be
pointed at Polymarket at all — not for trading, and not for read-only market data. Kalshi
is unaffected and remains usable on its own terms.

## Polymarket — professional and proprietary trading (decisive)

Prohibited conduct includes:

> "Access or use any data, content, or information contained on our Site, any Interface or
> Features directly or through an API, or any other means, including on-chain, whether in
> raw, derived, aggregated, or anonymized form (the "Data") if you are (i) a non-retail,
> professional entity that engages in capital markets activities (e.g., brokerage, market
> making, **proprietary trading**, index calculation, or ETF issuance), including but not
> limited to broker-dealers ... hedge funds, investment managers, **proprietary trading
> firms** ... or (ii) a market data distributor, in each case **unless otherwise agreed to
> in writing by us**"

A systematic cross-venue arbitrage operation is a plausible description of proprietary
trading. Whether that makes the operator a "non-retail, professional entity" turns on who
is running it: an individual retail trader is a different matter from a firm. If a firm,
API access to Polymarket Data requires **written agreement** first.

Note also the redistribution bar: Data may not be sold, sublicensed, redistributed, or
commercially exploited to any Capital Market Client or market data distributor.

## Polymarket — automated data gathering

> "Use any data mining tools, robots, crawlers, or similar data gathering and extraction
> tools to scrape or otherwise remove data from the Site, any other Interface, or
> Features"

and

> "Use any manual process to monitor or copy any of the material on the Site, any other
> Interface, or Features or for any other unauthorized purpose **without our prior written
> consent**"

The Terms expressly bind API connections ("BY ACCESSING ... INCLUDING BY CONNECTING VIA AN
API"). There is real tension between this clause and Polymarket's own published API
documentation, which plainly contemplates programmatic access. That tension is not ours to
resolve by assumption. The Terms do reference a **builder program** and reserve the right
to grant access; that is the route to seek explicit permission.

**Design response:** persistence of Polymarket data is bounded by the same retention
window already applied for Kalshi, and the same third-party sharing block applies to
webhook payloads. Neither cures the underlying permission question.

## Outstanding human gates

- [ ] **Confirm the operator's jurisdiction.** If anyone operating, directing, or
      controlling this system is in the United States or another listed territory, do not
      point it at Polymarket at all. There are no exceptions and no technical workaround
      is permissible.
- [ ] **Confirm retail status.** If the operator is a firm engaged in proprietary trading,
      Polymarket API access requires written agreement first.
- [ ] **Consider seeking written consent** for automated access via Polymarket's builder
      program, given the robots/scraping clause binds API connections.
- [ ] Confirm your Kalshi membership is in good standing and the Rulebook permits your
      intended automated activity.
- [ ] If you ever want to share output — a dashboard link, an export, an alert to a team
      channel — get **prior written authorization from Kalshi** first. §3.1 has no
      de minimis exception.
