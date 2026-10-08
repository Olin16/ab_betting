# Supervised live-data trial — launch package

Everything the operator needs to authorize, run and report the first live-**data** paper
trial, with the engineering side already done and the human side left explicitly open.

This document does not authorize anything. It is the package an operator reviews and then
either authorizes or does not. Steps marked **[operator]** cannot be done by anyone else:
they need a person's judgement, a person's name against a record, or contact with a venue.

- Procedure: [`docs/paper-trial-runbook.md`](paper-trial-runbook.md)
- Configuration: [`config/trial.env.example`](../config/trial.env.example)
- Engineering status: [`docs/trial-readiness.md`](trial-readiness.md)

## What is ready

| Item | State |
| --- | --- |
| Paper-only posture, structurally enforced | Ready — no execution adapter can place an order; `doctor` verifies the refusal |
| REST-only Kalshi production + Polymarket US configuration | Ready — `config/trial.env.example`, verified to load |
| Dedicated trial database at schema head | Ready — `alembic upgrade head`; expected revision `6b2f1c9a7d40` |
| Startup gates | Ready — the live preflight blocks an unscoped run before any venue request, and blocks a paper-trading run with no approved pair |
| Durable accounting across failure and restart | Ready — see the runbook §Appendix A and `docs/operations.md` §Accounting durability |
| Watchdog, stop/restart, exit codes | Ready — thresholds pinned in the configuration; exit codes in the runbook §5 |
| Reconciliation and reporting procedure | Ready — `event-arb reconcile`; reporting rules in the runbook §7 |
| Feed-health gating | Ready — refresh reports per-market coverage; preflight blocks a dead or unusable feed, and the runtime stops on one rather than reporting a quiet market |
| Durable run completeness | Ready — invalidated attempts and unfinished runs survive a restart in a journal beside the database; `acknowledge-run` clears one by appending, never by deleting |
| Independent trade/ledger reconciliation | Ready — each retained trade's cash re-derived from the trade rows and checked against the ledger; `reconcile` is genuinely read-only |
| Offline unattended-behaviour evidence | Ready — `tests/acceptance/`, **19** scenarios (8 legacy Global, 11 on the Kalshi + Polymarket **US** target), all reconciling |
| Executable launch procedure | Ready — `scripts/run-paper-trial.sh` captures the trial's real exit code; `event-arb manifest` identifies the build by content |
| **The market pair** | **Not ready — [operator]** |
| **Rulebook review and approval** | **Not ready — [operator]** |
| **Venue eligibility and terms confirmations** | **Not ready — [operator]** |
| **Current venue behaviour** | **Not ready — needs re-validation at run time** |

## Before authorizing

### 1. Operator confirmations **[operator]**

- [ ] Polymarket **US** onboarding complete; current terms reviewed; state eligibility
      confirmed; any required production API or conformance approval obtained in writing.
      Polymarket Global is not a fallback and this trial must not select it.
- [ ] Kalshi membership in good standing, and the Rulebook permits the intended automated
      activity. The Developer Agreement §3 limits API use to facilitating your own trading;
      §3.1 forbids sharing API data with third parties; §3.5 forbids availability
      monitoring and benchmarking. See `docs/compliance.md`.
- [ ] Rate limits for your actual access tier confirmed. The client's default 5 req/s per
      venue is a conservative ceiling, not a measured one.
- [ ] You accept that this run makes real, read-only requests to two production venues.

### 2. Identify the pair **[operator]**

Find one pair of markets that resolve to the same real-world event under both rulebooks.
`event-arb candidates discover` produces a capped shortlist; it never approves a pair and never reads a book.

If no genuinely equivalent pair exists, **that is the finding.** Report it. Do not widen
discovery, lower the match threshold, or relax rule comparison to produce a candidate —
a pair that is not equivalent is not a hedge, and a trial run on one measures nothing
except how convincingly the tooling can be talked into agreeing.

Record in `$EVIDENCE/pair.md`:

```markdown
# Pair under trial — <run id>

## Markets
- Kalshi ticker            : <TICKER>            (URL read: <url>, on <date>)
- Polymarket US condition  : <0x…>               (URL read: <url>, on <date>)

## Rulebooks (both read in full)
- Kalshi settlement source     : <authority, document title, URL, date read>
- Polymarket US settlement src : <authority, document title, URL, date read>
- Do both legs resolve to the same authority?     yes / no  — if no, stop.
- Close / expiry times          : Kalshi <…>  vs  Polymarket <…>   (differ? by how much?)

## Outcome alignment
- Does Polymarket YES correspond to Kalshi YES or to Kalshi NO?   <YES / NO>
- Therefore `outcomes_inverted` is: <false / true>
- Evidence for that determination: <quote the two outcome definitions>

## Rule differences reported by `event-arb pairs`
| Dimension | Kalshi | Polymarket | Material? | Why acceptable |
| --- | --- | --- | --- | --- |

## Fee assumptions
- Kalshi: rate and multiplier for this market, and where read.
  The model (`src/event_arb/venues/kalshi/fees.py`) was written against the fee-rounding
  page as read 2026-08-21 — re-verify it is unchanged.
- Polymarket US: taker theta 0.06, banker's rounding to the cent, volume rebates
  deliberately not netted in. Model written against docs.polymarket.us/fees as read
  2026-08-23 — re-verify it is unchanged.
```

`outcomes_inverted` is the single field that pays a settlement the wrong way round if it
is wrong, and the error is invisible: a plausible number lands in a plausible account.
Get it from the two outcome definitions, never from the titles.

### 3. Re-validate venue behaviour **[operator]**

The August 26 smoke-test attempt failed after two catalogue requests and left the book
criteria unmeasured; its evidence is history, not a current statement about either venue.
Re-run the bounded smoke workflow (`docs/smoke-test-plan.md`) at the time of the trial.
Note what that plan says about scope: substituting hand-entered market ids for its
catalogue-discovery step "requires a new explicit decision". Running it against the two ids
chosen above **is** such a substitution, so record the decision — who made it, when, and
that it is deliberately narrower than the original plan — rather than treating it as the
same test.

### 4. Approve the pair **[operator]**

Through the dashboard, after reading both rulebooks — `POST /api/pairs/{id}/approve` with
`approved_by`. Configuration cannot do this: watching a pair is not approving it, and the
preflight blocks a paper-trading run that has no approved pair rather than reporting an
empty trial as a completed one.

The approval record is persisted with the reviewer's name and the verified settlement
sources, and is re-verified against the rulebook hashes on every restart; a rulebook that
changes mid-trial suspends the pair rather than silently keeping the approval.

## The run

Follow [the runbook](paper-trial-runbook.md) §§0–7. In outline:

```bash
export RUN_ID="trial-rest-us-v1-$(date -u +%Y%m%dT%H%MZ)"
export EVIDENCE="evidence/$RUN_ID"

cp config/trial.env.example config/trial.env   # fill in the two market ids + API token
git check-ignore -v config/trial.env           # confirm it is ignored before adding a token
set -a && . ./config/trial.env && set +a

.venv/bin/alembic upgrade head

# live: both of these reach a venue once the live config is sourced.
EVENT_ARB_REVIEW_ONLY=1 .venv/bin/event-arb serve   # approve the pair, then stop
# ... approve via the authenticated curl the pair review page prints ...

# live: the run. The script captures the trial's own exit code, not tee's.
TRIAL_DURATION=2700 scripts/run-paper-trial.sh "$EVIDENCE" -- --rest-only

.venv/bin/event-arb settle --list                   # then settle, per runbook §6
.venv/bin/event-arb reconcile --json "$EVIDENCE/07-final.json"
```

Duration: 45 minutes, inside the agreed 30–60 minute band.

Three things changed here and each is load-bearing:

**`EVENT_ARB_REVIEW_ONLY=1` on the review `serve`.** The example configuration enables the
trial, so a plain `serve` against a database holding an approved pair starts paper trading
as a side effect of opening the dashboard — and a second runner if a CLI trial is already
going. Review-only cannot start one, whatever the trial config says, and still runs the
static scope and schema checks.

**The script instead of a bare `trial | tee`.** `$?` after a pipeline is *tee's* status,
and tee succeeds whenever it can write the file — so a trial that exited 1 on a watchdog
stop recorded `exit: 0` in its own evidence. The script reads `PIPESTATUS[0]`, exits with
the trial's status, writes the manifest and doctor output, reconciles afterwards, and
refuses to overwrite an evidence directory that is already populated.

**`event-arb manifest` rather than a `git status` hash.** The script runs it for you. See
the note under "What to hand back".

## What to hand back

A single `$EVIDENCE/report.md` containing, in this order:

1. Run identity and `00-manifest.json` — the **content** digest of every source file,
   tracked or not, plus the commit, the dirty flag and the schema revision. Do not use
   `git status --porcelain | shasum`: it hashes filenames and status letters, so two trees
   whose uncommitted files differ in every line hash identically, and most of this
   implementation is uncommitted. Do not hash `config/trial.env` into evidence either; the
   manifest deliberately excludes it by name and says that it did.
2. **Whether the accounts reconcile**, and **whether trade/ledger integrity is sound**.
   These are two different questions — the per-account arithmetic sums the same ledger on
   both sides, so it cannot see a trade whose cash was deleted. Nothing below means
   anything until both are yes.
3. **`paper_trades_invalidated`.** Non-zero means the execution record has a hole in it:
   those attempts are in no count, no balance and no rate. This now survives a restart.
4. **Any unfinished run**, and if you acknowledged one, who did and what they found.
5. **`refreshes_incomplete` and `feed_was_healthy`.** An outage is not a quiet market, and
   a run with incomplete refreshes did fewer scans than its duration implies.
6. How many trades happened. A no-trade run is a real result — it validates that ingestion,
   detection, the capital gate, the watchdog and persistence ran unattended for the
   duration, and validates nothing about fills, hedge quality or settlement. Say both.
7. The evidence grade of every trade, quoting `evidence_caveat` verbatim. Keep measured,
   unchanged-reread and modelled separate; do not average across them.
8. Whether settlement actually occurred. If the market had not resolved, settlement is
   **pending** — do not report the execution-time estimate as realised P&L.
9. Every stop cause, from `stopped_reason` and `last_error`.
10. The completed `pair.md` and the approval record.

## The remaining-input checklist

Everything below is something no code can supply. Nothing else blocks the run.

**Operator inputs — six items.**

1. **Two real market ids** — a Kalshi ticker and a Polymarket **US** condition id, each
   with the URL you read it from. If no equivalent pair exists, that is the finding:
   report it rather than loosening matching to produce one.
2. **Both rulebooks read in full**, with settlement source, close/expiry times, and every
   material rule difference recorded in `$EVIDENCE/pair.md`.
3. **Outcome alignment** — whether Polymarket YES is Kalshi YES or Kalshi NO. This is the
   `outcomes_inverted` flag and it is the one field that pays a settlement the wrong way
   round if it is wrong. Both directions are exercised offline; neither can tell you which
   one your pair is.
4. **Current fee schedules for both venues**, and the rate limits for *your* access tier.
   The configured 5 req/s ceiling is a conservative guess; the measured budget is ≤ 720
   requests per venue per hour for one pair at a 5 s cadence, plus two per simulated trade.
5. **Venue eligibility confirmations** — Polymarket US onboarding, terms, state
   eligibility, any written production approval; Kalshi membership in good standing.
6. **Authorization for the bounded live-data run itself.** This is a separate decision from
   "start the trial", because it is the first time this work contacts a production venue.

This package does not independently establish current venue terms, availability or fees.

## Exact commands for the first supervised run

Copy-pasteable. The four marked **live** reach a production venue and need item 6 above.

```bash
# --- 0. Identity and evidence directory ------------------------------------
cd /Users/owenlin/Documents/PP/ab_betting
export RUN_ID="trial-rest-us-v1-$(date -u +%Y%m%dT%H%MZ)"
export EVIDENCE="evidence/$RUN_ID"

# --- 1. Configuration ------------------------------------------------------
cp config/trial.env.example config/trial.env
git check-ignore -v config/trial.env          # MUST print a .gitignore line
$EDITOR config/trial.env                      # the two market ids; generate the API token
set -a && . ./config/trial.env && set +a

# --- 2. Dedicated database at head -----------------------------------------
# Never point a trial at ./data/event_arb.db — it is two migrations behind.
.venv/bin/alembic upgrade head
.venv/bin/python -c "
import sqlite3; c = sqlite3.connect('data/trial-rest-us-v1.db')
print(c.execute('select version_num from alembic_version').fetchone())"
# Expect: ('6b2f1c9a7d40',)

# --- 3. Posture check, before any venue request ----------------------------
.venv/bin/event-arb doctor
# live trading possible : False   |   selected ingestion : rest_polling
# polymarket product    : us      |   freshness budget   : OK

# --- 4. Review and approve the pair ---------------------- live, read-only --
EVENT_ARB_REVIEW_ONLY=1 .venv/bin/event-arb serve
# Open http://127.0.0.1:8000/pairs, read BOTH rulebooks, fill $EVIDENCE/pair.md,
# then run the authenticated curl the pair review page prints for your pair id.
# REVIEW_ONLY is required: this config enables the trial, so a plain `serve`
# would start paper trading as a side effect of opening the dashboard.
# Confirm the pair reads `approved` before stopping the server.

# --- 5. The run ------------------------------------------ live, read-only --
TRIAL_DURATION=2700 scripts/run-paper-trial.sh "$EVIDENCE" -- --rest-only
# 45 minutes. Exits with the TRIAL's status, not tee's. Writes:
#   00-manifest.*  01-doctor.txt  02-trial.log  03-reconciliation.json
#   04-reconcile.*  05-result.txt

# --- 6. Settle, if the market resolved --------------------------------------
.venv/bin/event-arb settle --list
.venv/bin/event-arb settle --trade-id <id> --kalshi-outcome YES \
    --recorded-by "<your name>" --evidence "<authority and document>" --dry-run
# Check the printed `outcome alignment` line, then re-run without --dry-run.

# --- 7. Final reconciliation ------------------------------------------------
.venv/bin/event-arb reconcile --json "$EVIDENCE/07-final.json"
echo "reconcile exit: $?"      # 0 is required for the run to be evidence
```

Stop and do not report a result if: `reconcile` exits non-zero;
`trade/ledger integrity` is not `sound`; `paper_trades_invalidated` is above zero;
`feed_was_healthy` is `false`; or an unfinished run is outstanding. Each has a named
remedy in the runbook — §6 for the first three, §7 for reporting, and
`event-arb acknowledge-run` for the last.

## After the run

A 24–72 hour observation is a **separate** authorization, not a continuation. Its exit
criteria are in the runbook §8. Review the supervised run against them first.

## Out of scope, deliberately

Real orders; WebSocket ingestion on either venue; Polymarket Global; additional venues;
venue-wide discovery (pagination is unimplemented); automated approval; automated
settlement ingestion. None of these is needed for a REST-only paper trial, and each adds a
surface that would have to be reviewed before it could be trusted.
