# Why the bounded catalogues produced zero candidates — 2026-08-26

**No request was made for this investigation.** Everything below comes from the preserved
code, the recorded catalogue fixtures, the dedicated smoke database, and offline
computation over the scoring pipeline.

The 2026-08-26 smoke test sent its two catalogue requests, parsed 250 markets from each
venue with zero mapper failures, ran all 62,500 local comparisons, and retrieved **zero**
candidates at `min_score=0.55`. It required two and stopped. This is the diagnosis and the
options for a revised plan.

---

## 1. What was eliminated first

`select_smoke_candidates` ends in an unconditional fallback that takes the highest-scoring
remaining records regardless of style, so a "Fed plus weather/CPI" preference cannot yield
zero when records exist. Zero selected therefore means **zero records**, and with a fresh
database there were no prior human dispositions and no mapper failures to filter them. The
failure is entirely in scoring against the threshold — not in selection, not in the style
preference, not in persistence.

Ruled out by inspection:

| Hypothesis | Verdict |
| --- | --- |
| Style preference (Fed/weather/CPI) filtered everything | **No** — the fallback loop ignores style |
| Product/schema mapping defect | **No** — zero mapper failures, both checked schemas matched |
| Missing filters on the requests | **No** — both venues applied the documented params and returned 250 |
| Prior dispositions suppressed pairs | **No** — the database was new; `preserved_dispositions=0` |
| Retained-result cap (`max_results=100`) | **No** — the cap truncates, it cannot empty |

## 2. The primary cause: a 25%-weight signal that is inert on live data

`score_pair` blends four signals — token overlap (0.30), fuzzy title ratio (0.20), date
agreement (0.25), threshold agreement (0.25).

`extract_dates` deliberately ignores a date with no explicit year, because assuming the
current year would pair a 2026 contract with a 2027 one. That is correct. But **live venue
titles seldom state a year.** The recorded 2026-08-23 catalogues confirm the shape: Kalshi
returned `"yes Ryota Tanuma,yes Chen Hui Ho,…"`; Polymarket US returned
`"National League Champion"`. Neither carries a year, and neither would in the ordinary
case either — `"Fed decision in September?"` does not.

So on live data the date signal almost always resolved to `None` and was neutral-imputed at
0.5, contributing a fixed 0.125 regardless of the markets. The same is often true of the
threshold signal. That leaves the ceiling analysis below, computed offline:

| Regime | Ceiling | Floor | Surface mass needed to reach 0.55 |
| --- | --- | --- | --- |
| dates agree, thresholds agree | 1.0000 | 0.5000 | 0.05 of 0.50 available |
| dates agree, thresholds absent | 0.8750 | 0.3750 | 0.175 |
| dates absent, thresholds agree | 0.8750 | 0.3750 | 0.175 |
| **dates absent, thresholds absent** | **0.7500** | **0.2500** | **0.300** |
| either signal *contradicts* (×0.35) | 0.3500 | 0.1750 | unreachable at any wording |

In the common live regime — both structural signals absent — a pair must draw 0.30 of a
maximum 0.50 from surface wording alone. Cross-venue phrasings of the *same* question
typically score Jaccard 0.25–0.40 and fuzzy 0.72–0.78, which is roughly 0.25–0.28. They
land just below the line, systematically:

| Same underlying question | Score | vs 0.55 |
| --- | --- | --- |
| "Will the Fed cut rates by 25bps at the September 2026 meeting?" / "Fed cuts rates by 25 bps in September 2026?" | 0.5316 | fail |
| "Will the Fed cut rates at the September meeting?" / "Fed decreases interest rates by 25 bps after September meeting?" | 0.5243 | fail |
| "Fed decision in September?" / "Fed cuts rates in September?" | 0.5261 | fail |
| "NYC high temp on Aug 22?" / "Highest temperature in NYC on August 22?" | 0.4734 | fail |
| "Who will win the 2026 World Series?" / "National League Champion" | 0.4955 | fail |

The first row is this project's own canonical Fed fixture — the style the smoke test
explicitly prefers by name — scoring **0.5316** against a 0.55 gate.

## 3. Why the test suite did not catch it

`min_score=0.55` was asserted as a *number* and never exercised as *behaviour*:

- `test_scope_matches_the_documented_initial_test_bounds` asserts `LIMITS.min_score == 0.55`.
- Every other test in `test_candidate_discovery.py` that generates records passes
  `min_score=Decimal("0")`.
- The fixture end-to-end path (`main.py`) runs `generate_pairs(min_score=Decimal("0.4"))`.
- `generate_candidates` and `Scanner.generate_pairs` both default to `0.5`.

Four thresholds — 0.4, 0.5, 0.5, 0.55 — and the canonical Fed pair at 0.5316 sits inside
the gap. Everything passed at 0.4; the live run was the first execution at 0.55.

## 4. Contributing cause: catalogue composition and ordering

`GET /markets?status=open&limit=250` applies no subject filter and no ordering the client
controls. The recorded catalogue's first entry is a `KXMVECROSSCATEGORY` multivariate
parlay shard whose title is a list of parlay legs. Nothing in the mapper or the runner
excludes these, so they consume the 250-market budget and can never match anything on
Polymarket US. The recorded Polymarket US catalogue opens on sports futures.

This is a real and separate constraint on any revised plan: **the first 250 open markets on
each venue are not a subject-aligned sample**, and Kalshi returned a cursor, which was
correctly counted and not followed. Its magnitude cannot be measured offline — that would
take a live request — so it is stated as a risk, not quantified.

## 5. Fix applied offline (no request)

Commit `d0e79e9` lets the venue's own close time carry the date signal when neither title
states a year. Both mappers already populate `close_time` from data both venues always
publish (Kalshi `close_time`, Polymarket US `endDate`) on the object the scorer receives.

Corroboration only, deliberately asymmetric:

- identical close **day** with no prose dates → date agreement;
- close-time **mismatch** → back to the neutral imputation, *never* the contradiction
  penalty, because venues legitimately close the same question at different times;
- a stated **prose** date conflict → still the hard ×0.35 penalty, unchanged;
- a shared close **alone** cannot carry a pair — unrelated contracts routinely settle the
  same day, and surface wording must still agree.

Measured offline on the reference corpus: the canonical Fed pair moves 0.5316 → 0.7816, and
retrieval at 0.55 goes from one pair to the two the smoke test requires. Six regression
tests now exercise the production threshold, including one that fails if live-shaped titles
stop retrieving at 0.55.

**This changes local scoring only. Request scope, endpoints, params and budget are
untouched.** It does not by itself justify another live run.

---

## 6. Options for a revised plan

Ranked smallest-change first. **None has been executed. Each needs explicit approval.**

### Option A — re-run §1 unchanged, on the corrected scorer

| | |
| --- | --- |
| Endpoints | §1 rows 1–5, unchanged |
| Params | `status=open&limit=250`; `active=true&closed=false&limit=250` |
| Worst case | **31** requests — identical to the approved plan |
| Pagination | not followed; a cursor is counted and reported |
| Data retained | unchanged: selected legs, ≤4 book snapshots, audit rows |
| Compliance | no change; same two public hosts, same read-only GETs |
| Changes the approved plan? | **No.** §1, §2 and §3 are byte-identical |
| New approval required | Re-authorization to transmit, and acknowledgement that the local scorer changed since the 2026-08-24 approval |

The honest option. It tests the actual hypothesis — that the threshold, not the universe,
was the binding constraint — at zero additional request cost. It fails the same way if
§4 (catalogue composition) is the real constraint, which is itself the next piece of
evidence.

### Option B — Option A plus one Kalshi cursor page

| | |
| --- | --- |
| Endpoints | §1 rows 1–5, plus one repeat of row 1 with `cursor={returned}` |
| Worst case | **32** requests (§2's total rises by 1) |
| Pagination | exactly one page followed, then stopped; a second cursor is counted, never used |
| Data retained | unchanged |
| Compliance | still public read-only GETs; volume rises ~3% against a 5 req/s bucket |
| Changes the approved plan? | **Yes** — §1 row 1 count, §2 total, and §2's "pagination is not followed" |
| New approval required | Full re-approval of §1 and §2 |

Doubles the Kalshi sample against the §4 risk. It costs the plan's cleanest safety
property — that pagination is never followed — for one page. Not worth it before Option A
has told us whether the universe was even the problem.

### Option C — lower only the local threshold, no request change

| | |
| --- | --- |
| Endpoints | §1 unchanged |
| Worst case | **31** |
| Data retained | unchanged |
| Compliance | no change whatsoever |
| Changes the approved plan? | **No** to §1/§2; **yes** to the documented `min_score` |
| New approval required | Approval of the new threshold value |

Note this is **not a runtime flag**: `smoke_test.py` hardcodes `DiscoveryLimits(recheck_top=25)`,
so any threshold change is a code change and an auditable diff. Offline sweep of the
threshold against a hand-labelled set: 0.55 keeps 2 of 8 true matches, 0.50 keeps 5, 0.45
keeps 7. But lowering the gate widens what a human is asked to review rather than improving
what retrieval knows, and the §5 fix already addresses the cause. **Not recommended on its
own**; if it is wanted, it should follow Option A's evidence, not precede it.

### Option D — explicitly supplied public market IDs

| | |
| --- | --- |
| Endpoints | `GET /trade-api/v2/markets/{ticker}` ×2, `GET /v1/market/slug/{id}` ×2, then §1 rows 4–5 |
| Params | none beyond the path |
| Worst case | **8** requests — the smallest plan that can reach a book |
| Pagination | none; no catalogue listed at all |
| Data retained | the 2 named pairs and ≤4 book snapshots only |
| Compliance | narrowest surface of any option; touches only markets the operator names |
| Changes the approved plan? | **Yes, fundamentally** — §1 rows 1–2 are dropped and the market scope stops being derived |
| New approval required | A new plan document, plus the operator supplying the four ids |

Both adapters already support this: `KalshiClient._fetch_markets_by_ticker` and the
Polymarket US slug fetch retrieve named markets one request each. This is the only option
that reaches rows 4–5 with near-certainty, because it removes retrieval from the critical
path. It also stops testing whether retrieval works — which is a legitimate trade if the
goal is to unblock the **book schema** questions (success criteria 3, 5, 6), and a bad one
if the goal is to validate discovery.

### Recommendation

**Option A, then Option D if it fails.** Option A costs nothing beyond the already-approved
budget and discriminates the two live hypotheses: if the corrected scorer retrieves two
pairs, the threshold was the binding constraint and step 7 completes on the approved plan;
if it still retrieves zero, §4 is confirmed and Option D is the minimal way to get the book
schemas measured while catalogue composition is dealt with separately.

Do not run Option B or C first. Both spend an approval, and neither answers the question
Option A answers for free.

## 7. What remains unmeasured

Unchanged by this investigation. No book was fetched, so the current Kalshi production
order-book schema, the Polymarket US public book schema, two-sided normalization on real
candidates, non-empty executable asks, four-book refresh timing, live depth, adverse leg-2
movement, hedge success, profitability and settlement equivalence all remain unknown.
The paper hedge rate is still correctly reported as unmeasured.
