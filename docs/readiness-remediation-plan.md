# Readiness Remediation — Plan and Acceptance Tests

Addresses findings F1–F10 from the readiness assessment, in milestone order. Each finding
lists the acceptance tests that must pass before it is considered done.

**Scope guards for all of this work:** no live venue calls, no Phase 7, no order path.
`live_trading_possible` stays `False`.

## Hostname correction (pre-work)

The assessment proposed `api.elections.kalshi.com`. That was wrong. Kalshi documents
`external-api.kalshi.com` as **recommended** — *"The `external-api` hosts are dedicated to
the external Trade API and are the recommended hosts for API traders"* — with
`api.elections.kalshi.com` as the legacy shared host kept for compatibility. The repository
already uses the recommended host and **is not changing**. The earlier one-off smoke test
used the legacy host; future plans use the configured recommended one.

---

## Milestone 1 — before a bounded live candidate run

### F3/F4 — the request plan must be the requests

The plan is what the operator authorises, so it must be derived from the constructed
adapters rather than written independently. Anything that cannot be performed is removed
from the plan rather than described.

- Plan is built **from adapters**: endpoints, params, product and authentication state.
- The credential-free command **refuses** if credentials are configured, unless an
  explicitly named authenticated mode is selected.
- Global selection can never display or call US endpoints.
- Targeted verification is **implemented** (fetch, mark `verified`) — not advertised and skipped.
- Only the capped shortlist is persisted.
- Existing dispositions are **restored before** generation.
- Cursors are detected and reported, never followed.
- The nonexistent "re-run with the venue selected" instruction is removed.

**Acceptance tests**
1. Rendered plan URLs equal the URLs the adapters actually request (asserted against a
   recording transport).
2. `product=global` renders and calls Global hosts only; no `polymarket.us` string appears.
3. Configured Kalshi credentials → command aborts with a named error; authenticated mode
   required to proceed.
4. Plan declares auth state truthfully in both cases.
5. Verification issues exactly `min(verify_top, shortlist)` requests and sets `verified`.
6. Shortlist persisted; count ≤ `max_results`; raw bodies absent.
7. Dispositions loaded from a database are preserved across a run.
8. A cursor in the response increments `pagination_cursors_ignored` and is not followed.

### F8 — CLI integration tests
Cover confirmation, abort, adapter/product selection, plan rendering, persistence, and that
fixture mode makes zero network calls.

### F10 — documentation truth
Correct stale status lines; document `candidates discover` **only after** behaviour matches.

---

## Milestone 2 — before a supervised smoke test

### F1 — canonical settlement-source identity

Not a justification override. A reviewer records, **per leg**, a *canonical authority* plus
supporting evidence. Approval proceeds only when both legs resolve to the **same** verified
authority. Different authorities stay unapprovable and research-only — there is no override.

- `MarketPair` gains per-leg canonical source id + evidence.
- Canonical source enters the **v4** fingerprint.
- A changed or missing source after restart **suspends** the approval.

**Acceptance tests**
1. Two different canonical authorities → refused, no override accepts them.
2. Same canonical authority on both legs → approved.
3. Canonical source is in the fingerprint (v4); changing it changes the hash.
4. Restart with a changed source suspends; restart with missing evidence suspends.
5. Sources survive a database round trip and are visible for review.

### F7 — measured freshness
Freshness is derived from **observed** refresh-batch duration and receipt timestamps, not a
guessed constant. A strict configurable ceiling remains, and the budget check warns when the
watched-pair request count cannot meet it.

**Acceptance tests:** one pair; multiple serially-refreshed pairs; slow responses; a scan
immediately after refresh; and a budget that cannot be met warns rather than silently passing.

---

## Milestone 3 — before an unattended paper trial

### F2 — complete fee persistence
Persist `fee_policy`, `maker_rebate_rate`, `source` and every calculation-relevant field.
The migration **invents nothing**: product identity is backfilled only where invariant, and
fee policy for pre-existing rows is marked **unknown**, requiring fresh venue metadata
before pricing.

**Acceptance tests:** full persist → restart → scan; unknown policy refuses to price and
says why; migration up/down/up.

### F5 — product identity everywhere
Product joins repository keys, in-memory keys, pair IDs, uniqueness constraints, snapshot
indexes and restore. Both histories coexist. The pair-ID change is **approval-invalidating**
and forces re-review.

**Acceptance tests:** both products survive `load_all`; distinct pair ids; snapshot history
separated; old approvals require re-review after the id change.

### F6 — honest execution latency
Live REST paper trading fetches a **new rate-limited snapshot at each leg's arrival**,
especially leg 2. Those extra requests are declared in docs and counted in metrics. Fixture
and replay paths use timestamped frames or an explicitly calibrated adverse-movement model,
and results are labelled so an unchanged re-read is never reported as measured hedge-failure
evidence.

**Acceptance tests:** a book changing mid-latency yields a partial fill; extra requests
counted; fixture mode labels its evidence basis.

### F9 — test typing
A separate mypy pass over tests. Application strictness is not weakened to accommodate it.
