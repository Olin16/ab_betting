# Market Matching

## The core claim this system refuses to make automatically

Two markets with near-identical titles are **not** necessarily the same contract. Title
similarity is a retrieval signal. Settlement equivalence is a legal and operational
property of two rulebooks, and no scoring function establishes it.

Consider the pair shipped in `tests/fixtures/`:

| | Kalshi | Polymarket |
| --- | --- | --- |
| Title | "Will the high temperature in NYC on Aug 22 2026 be above 90F?" | "Will NYC hit 90F or higher on August 22, 2026?" |
| Threshold | 90°F | 90°F |
| Comparison | **strictly greater** | **greater or equal** |
| Source | NWS Central Park, **preliminary daily** | NOAA **final corrected monthly** |

Fuzzy title similarity ≈ 0.76. Same city, same date, same number. But at exactly 90.0°F
they settle to **opposite** outcomes, and if the preliminary report says 90.4 and the
final correction says 89.8, they settle to opposite outcomes again. A "hedge" here is a
leveraged directional bet on a data discrepancy.

The scanner surfaces this pair with an attractive-looking $12.63 net profit — and labels
it a **research candidate**, flags `rules_differences_present`, and refuses approval
without explicit acknowledgement of the material differences.

## Two-stage pipeline

### Stage 1 — Candidate generation (machine)

`matching/candidates.py` scores each cross-venue pair in [0, 1]:

| Signal | Weight | Notes |
| --- | --- | --- |
| Token overlap (Jaccard) | 0.30 | Stopword-filtered content tokens |
| Fuzzy title ratio | 0.20 | `rapidfuzz.token_set_ratio` |
| Date agreement | 0.25 | Extracted ISO and "Aug 22 2026" dates |
| Threshold agreement | 0.25 | Extracted numeric threshold |

A **contradiction** on dates or thresholds multiplies the score by 0.35 rather than
deducting a little — those are exactly the cases where surface similarity misleads most.

Extraction is conservative and returns `None` rather than guessing. Three specific traps
are handled, each of which produced a wrong threshold before being fixed:

- **Idioms containing comparison words.** "year-**over**-year" is not `>`.
- **Trailing operators.** "90F or higher" puts the number before the operator; "above 90F"
  after. The number *nearest* the operator wins.
- **Years posing as thresholds.** In "above 3.0% for August 2026", `2026` is a date.
  Unit-less numbers matching an extracted year are skipped.

Dates without an explicit year are **ignored, not assumed** to be the current year.

### Stage 2 — Human approval (mandatory)

Every generated pair is `suggested` with `manually_approved=False`. Nothing in the
matching code can produce an approved pair.

`matching/approval.py` requires:
- an identified reviewer (`approved_by` must be non-empty);
- explicit acknowledgement when material differences exist;
- storage of **both rules hashes** at approval time.

## Compared dimensions

`matching/rules_diff.py` produces a structured, side-by-side diff. Material differences
can change which side settles in the money.

| Dimension | Material | Why |
| --- | --- | --- |
| `threshold` | yes | Different numbers are different questions |
| `comparison_operator` | yes | `>=` pays at the boundary, `>` does not |
| `threshold_unit` | yes | Not directly comparable |
| `close_time` | yes | One venue trades on information the other cannot |
| `settlement_source` | yes | Sources disagree |
| `revision_policy` | yes | A correction can settle both legs the same way |
| `postponement` | yes | Resolve-No vs void-and-refund differ economically |
| `invalid_outcome` | yes | 50/50 vs void vs No |
| `timezone` | no | Informational; compare absolute instants |
| `early_closure` | no | Documentation asymmetry |
| `settlement_asset` | no | Global uses USD/USDC with a haircut; Polymarket US and Kalshi both use USD |

### Equivalence classes, not phrasings

Revision policy is classified into **equivalence classes** — `first print governs` and
`final/revised value governs` — rather than compared as text. "Subsequent revisions are
ignored" and "only the initial release is used" are the same policy; reporting them as a
difference would be a false positive. That matters beyond tidiness: a reviewer who sees
spurious "material" warnings learns to click past the real ones.

## Rule-change detection

The hash covers title + rules text + settlement source, whitespace-normalized and
lowercased, so reformatting does not cause churn while any substantive edit does.

`enforce_rules_integrity()` runs at the top of **every scan cycle**, before any pricing.
On a mismatch the pair is moved to `suspended` and `manually_approved` is cleared, so it
cannot satisfy `is_tradable_pair` even for a single cycle.

## Outcome inversion

Some equivalent pairs map Polymarket YES to Kalshi NO ("Team A wins" vs "Team B wins").
`MarketPair.outcomes_inverted` is explicit and set by a human. It is never inferred at
calculation time: getting it wrong buys the same side twice — a doubled directional bet
rather than a hedge.

## Shipped fixtures

Three example pairs, none asserted as a live mapping:

1. **Fed September 2026** — genuinely equivalent, real edge → confirmed opportunity.
2. **NYC temperature** — misleadingly similar, two material differences → rejected.
3. **CPI YoY** — equivalent (including revision policy, once classified properly) but no
   economic edge → rejected on economics.
