# Generic Market Legs — Migration Analysis

**Status: analysed, not executed.** This documents what a `MarketLeg` / `CrossVenuePair`
migration would cost before anyone commits to it. Nothing in the sportsbook work required
it, which is itself the main finding.

## The coupling, measured

| Module | Kalshi/Polymarket references |
| --- | --- |
| `opportunities/calculator.py` | 104 |
| `opportunities/service.py` | 40 |
| `scanner.py` | 34 |
| `domain/models.py` | 19 |
| `domain/enums.py` | 12 |
| `persistence/repositories.py` | 11 |
| `persistence/models.py` | 7 |
| `venues/base.py` | **0** |

`venues/base.py` is already generic. The coupling lives in the layers *above* the adapter
interface, not in the adapter contract.

## Proposed target shape

```python
class MarketLeg:          # venue/provider identity, product identity, market id,
                          # outcome/selection, instrument type
class CrossVenuePair:     # two explicitly identified legs, approval state,
                          # per-leg settlement fingerprint, rule differences, notes
```

## What it would break

**Domain models.** `MarketPair.kalshi_market_id` / `polymarket_market_id` and
`Opportunity.kalshi_leg` / `polymarket_leg` become `legs: tuple[MarketLeg, MarketLeg]`.
Every construction site and every attribute read changes.

**`ArbDirection` stops making sense.** `A_KALSHI_YES_POLY_NO` names its venues. Generic
legs need direction expressed as per-leg outcomes, which changes a persisted enum value.

**Database.** `market_pairs` has product-named columns and a
`UniqueConstraint("kalshi_market_id", "polymarket_market_id")`. `opportunities` stores a
JSON snapshot whose shape is the current model. A migration must rewrite both, and the JSON
snapshots are not mechanically rewritable without a bespoke transform.

**Repositories.** `MarketPairRepository._to_domain` and `save` map field-by-field; both are
rewritten. `restore_pairs` matches on `(Venue.KALSHI, pair.kalshi_market_id)` and would key
on legs instead.

**Approval restoration.** The highest-risk area. Stored approvals carry
`kalshi_rules_hash_at_approval` / `polymarket_rules_hash_at_approval`. Under generic legs
these become per-leg fingerprints. A migration that mapped them wrongly could restore an
approval against the wrong leg — the exact failure the fingerprint exists to prevent.
**Mitigation: bump `SETTLEMENT_FINGERPRINT_VERSION` as part of the migration**, which
invalidates every stored approval and forces re-review. Costly for the operator, and
correct.

**API compatibility.** `/api/scan` returns `model_dump()` of `Opportunity`, so the JSON
shape changes for any consumer. `/api/pairs/{id}/approve` keeps its URL.

**Fixtures.** `tests/fixtures/` is keyed by venue directory and is unaffected. Test helpers
that build `MarketPair(kalshi_market_id=...)` — roughly 70 sites — all change.

**Metrics.** `opportunities_detected{direction=...}` label values change with
`ArbDirection`, breaking time-series continuity in any dashboard.

## Rollback strategy

1. Migration is reversible at the schema level (`downgrade` recreates the product-named
   columns), but **data rewritten into leg form cannot be perfectly restored** — the
   original column mapping is recoverable only if the migration writes both shapes during
   a transition window.
2. Therefore: land it as an **expand/contract** pair. Expand adds leg tables and populates
   them while leaving the old columns intact and authoritative. A later contract migration
   drops the old columns once the new path has run a full trial.
3. Between the two, rollback is a code revert with no data loss.

## Recommendation

**Do not migrate yet.** Three reasons:

1. **The sportsbook work did not need it.** Sportsbook data has its own models, its own
   adapter, and its own mapping table. Forcing it through a shared leg abstraction would
   have coupled an odds feed to an exchange order book — the thing this design explicitly
   avoids.
2. **The cost is concentrated in the riskiest code.** 144 references across the calculator,
   service and scanner, plus an approval-restoration rewrite whose failure mode is a
   wrongly-restored approval.
3. **The trigger has not arrived.** Generic legs earn their cost when a *third exchange*
   needs pairing against the existing two. Polymarket US replacing Global does not
   qualify — that is one venue swapping for another in the same slot.

**Trigger to revisit:** a concrete requirement to pair three or more exchanges, or to pair
two exchanges that are not Kalshi-and-one-other. At that point do it as expand/contract,
with a fingerprint-version bump, and as its own change with no adapter work bundled in.
