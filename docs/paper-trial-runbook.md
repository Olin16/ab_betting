# Supervised paper-trial runbook — REST-only, Kalshi + Polymarket US

The procedure for one supervised 30–60 minute live-**data** paper trial, and the exit
criteria for extending it. Every command here is copy-pasteable and was run as written,
except the four marked **live** — those reach a venue and need the operator's explicit
authorization, which no configuration file and no automated step can supply.

Nothing in this document authorizes a real order. This build's only execution adapter
refuses every submission unconditionally, `event-arb doctor` verifies that refusal at
runtime, and the trial preflight blocks if `live_trading_possible` is ever true.

- Configuration: [`config/trial.env.example`](../config/trial.env.example)
- Configuration identity: `trial-rest-us-v1`
- Acceptance evidence this procedure depends on: [offline fault-injection
  exercise](#appendix-a--the-offline-acceptance-exercise)

## 0. Record what you are running

The evidence is worthless if it cannot be tied to a revision. Do this first and paste the
output into the run's evidence directory:

```bash
.venv/bin/event-arb manifest --json "$EVIDENCE/00-manifest.json"
```

This hashes the **content** of every source file — `src/`, `tests/`, `alembic/`, `config/`,
and the named build files — whether or not git tracks it, and prints one overall digest
over the sorted `(path, digest)` pairs. It also records the commit, whether the tree is
dirty, which source files are untracked, and the effective configuration with every
`SecretStr` rendered as its type rather than its value.

The previous instruction here was `git status --porcelain | shasum -a 256`. That hashes a
list of *filenames and status letters*: two working trees whose uncommitted files differ in
every line produce the same digest under it. Most of this implementation is currently
uncommitted, which makes that the one case it cannot survive — and the case the evidence
most needs it for.

`config/trial.env` is **excluded by name and never read**, because the manifest is written
into the evidence directory. The manifest lists it under `excluded_secret_files` so the
record is honest about what it did not cover. Do not `shasum` that file into evidence
either; its digest is not worth the risk of the habit.

Reviewed revision for this runbook: branch `smoke-test-preflight-blocked`, base commit
`c4f0cf0`, plus the uncommitted durable-accounting work described in
[the handoff](claude-paper-trial-handoff.md). Expected schema revision: `6b2f1c9a7d40`.

Run identity is `<config id>-<UTC start>`, e.g. `trial-rest-us-v1-20260910T1600Z`. Create
its evidence directory now; everything below writes into it:

```bash
export RUN_ID="trial-rest-us-v1-$(date -u +%Y%m%dT%H%MZ)"
export EVIDENCE="evidence/$RUN_ID"
mkdir -p "$EVIDENCE"
```

## 1. Prepare the configuration

```bash
cp config/trial.env.example config/trial.env
$EDITOR config/trial.env          # fill in the two market ids; set the API auth token
set -a && . ./config/trial.env && set +a
```

`config/trial.env` is ignored by git — `.gitignore` names it exactly, because the
`.env.*` rule above it does not match a basename that does not begin with `.env`. The
committed `config/trial.env.example` is explicitly un-ignored beside it. Confirm both
before you put a token in the file:

```bash
git check-ignore -v config/trial.env          # expect a .gitignore line
git check-ignore -v config/trial.env.example  # expect the negation rule
```

The shipped market ids are placeholders (`REPLACE-ME-…`) and will not resolve at either
venue, so a run started before they are replaced fails at discovery rather than quietly
trading something nobody chose. Record, in `$EVIDENCE/pair.md`, where each id came from —
the Kalshi ticker and the Polymarket condition id, each with the URL you read it from.

## 2. Create a dedicated database at head

The existing `./data/event_arb.db` is at revision `3ea85fd4182a`, two migrations behind.
**Do not point a trial at it.** A trial must not be the thing that migrates a database:
if the migration goes wrong the run is lost and so is the file.

```bash
.venv/bin/alembic upgrade head
.venv/bin/python -c "
import sqlite3; c = sqlite3.connect('data/trial-rest-us-v1.db')
print(c.execute('select version_num from alembic_version').fetchone())"
```

Expected: `('6b2f1c9a7d40',)`. The preflight refuses to start against anything else.

## 3. Check the configuration before touching a venue

```bash
.venv/bin/event-arb doctor | tee "$EVIDENCE/00-doctor.txt"
```

Read four lines in that output and stop if any is wrong:

| Line | Expected for this trial |
| --- | --- |
| `live trading possible` | `False`, and `execution adapter : refuses all order operations (verified)` |
| `selected ingestion` | `rest_polling (credential-free)` |
| `polymarket product` | `us` |
| `freshness budget` | `OK` |

For one watched pair (two markets) `doctor` reports an estimated 0.80 s refresh batch and
a required book age of 1300 ms; the configured `max_book_age_ms=2000` clears it. Do not
raise that bound to accommodate a slow feed — a higher bound means acting on older prices,
and the check exists to catch exactly that trade.

**Request budget.** Measured at the adapter boundary in
`tests/integration/test_request_budget.py`, not estimated here — a change in request shape
breaks that file rather than quietly invalidating this paragraph.

| What | Kalshi | Polymarket US | Note |
| --- | --- | --- | --- |
| One book fetch | 1 HTTP GET | 1 HTTP GET | US serves both outcomes in one response |
| One refresh cycle, one pair | 1 | 1 | one market per venue |
| 60 minutes at a 5 s cycle | **≤ 720** | **≤ 720** | 3600/5 = 720 cycles, upper bound |
| Each simulated trade | +1 | +1 | the leg re-fetch, 2 per trade total |

At 5 s that is **0.2 req/s per venue** against a 5 req/s client ceiling — one request per
venue per cycle, not two. The previous figure here said 1,440 per venue per hour, which
came from counting the whole two-market batch against each venue. 720 is an upper bound:
the cycle sleeps 5 s *between* cycles and the batch takes time on top, so a real hour fits
fewer cycles rather than more.

The per-trade re-fetch happens because in REST polling nothing updates the store between
the legs, so re-reading it would grade a hedge against a book that could not have moved.
Watch `event_arb_paper_refetches_total`. Under WebSocket ingestion, and on fixtures, there
is no re-fetch at all.

**Polymarket Global costs two GETs per book**, one per outcome token. That is the legacy
path and not this trial's; do not carry its cost into this budget.

## 4. Approve the pair — the human step

Approval is a person's judgement and nothing else can supply it. The configuration file
cannot: watching a pair is not approving it, and the preflight blocks a paper-trading run
with no approved pair rather than reporting an empty trial as a completed one.

```bash
.venv/bin/event-arb pairs                          # live: sources the live config
EVENT_ARB_REVIEW_ONLY=1 .venv/bin/event-arb serve  # live: dashboard on 127.0.0.1:8000
```

Both of these reach a venue when the live configuration is sourced — `pairs` fetches the
metadata it compares, so it is a **live** command exactly as `serve` is. Earlier revisions
of this document labelled only `serve`.

`EVENT_ARB_REVIEW_ONLY=1` is not optional here. The example configuration enables the
trial, so a plain `serve` against a database that already holds an approved pair starts
paper trading as a side effect of opening the dashboard to look at it — and if you left a
CLI trial running, you now have two runners against one database. Review-only refuses to
start any trial whatever the trial configuration says, prints a banner on every page, and
still runs the static scope and schema checks underneath. Start the trial deliberately, in
§5, when you intend to.

Before approving, read **both** complete rulebooks and record in `$EVIDENCE/pair.md`:

- each venue's settlement source and the exact document consulted;
- the close/expiry times and whether they differ;
- every material rule difference `event-arb pairs` reported, and why it is acceptable;
- whether Polymarket YES corresponds to Kalshi YES or to Kalshi NO — this is the
  `outcomes_inverted` flag, and it is the one field that pays a settlement the wrong way
  round if it is wrong;
- the fee assumption for each leg, and where it came from.

Then approve. The pair review page prints the exact command for this deployment; with
`EVENT_ARB_API_AUTH_TOKEN` set — which this runbook recommends — it includes the bearer
token header, read from the environment rather than pasted:

```bash
set -a; . config/trial.env; set +a
curl -fsS -X POST http://127.0.0.1:8000/api/pairs/<pair-id>/approve \
  -H "authorization: Bearer $EVENT_ARB_API_AUTH_TOKEN" \
  -H 'content-type: application/json' \
  -d '{"approved_by":"you@example.com","reviewer_notes":"...",
       "kalshi_settlement_source":"...","polymarket_settlement_source":"..."}'
```

`-f` matters: without it curl exits **0** on a 401 or a 400, so a refused approval looks
exactly like an accepted one. Confirm the approval landed rather than trusting the exit
code alone — the pair should read `approved` in the dashboard. The approval, with the reviewer's name and the verified settlement sources, is
persisted and re-verified against the rulebook hashes on every restart — a rulebook that
changes suspends the pair rather than silently keeping the approval.

## 5. Run the trial

```bash
# live: this is the run
TRIAL_DURATION=2700 scripts/run-paper-trial.sh "$EVIDENCE" -- --rest-only
```

45 minutes sits in the middle of the 30–60 minute band. Start there.

Use the script rather than the bare command. This document used to say:

```bash
... 2>&1 | tee "$EVIDENCE/01-trial.log"
echo "exit: $?" | tee -a "$EVIDENCE/01-trial.log"      # WRONG
```

`$?` after a pipeline is the status of its **last** command, which is `tee`. tee succeeds
whenever it can write the file, so a trial that stopped on a watchdog failure and exited 1
recorded `exit: 0` in its own evidence, and the operator's only record of the run said it
finished cleanly. The script reads `PIPESTATUS[0]` instead and exits with the trial's own
status; `tests/integration/test_trial_script.py` drives it with a stub entrypoint to prove
a failing trial fails the script.

It also writes the manifest, runs `doctor`, reconciles from a fresh process afterwards, and
refuses to write into a directory that already holds evidence — two runs in one directory
become one unreadable record. Artifacts land as `00-manifest.*`, `01-doctor.txt`,
`02-trial.log`, `03-reconciliation.json`, `04-reconcile.*` and `05-result.txt`.

**Exit codes.**

| Code | Meaning | What to do |
| --- | --- | --- |
| 0 | Ran to its duration, loops healthy, accounts reconcile | Proceed to §6 |
| 1 | Watchdog stop, a dead background task, or a reconciliation that did not close | Do **not** read the figures as a result; see §7 |

Ctrl-C is an operator stop and exits 0 — an orderly shutdown is not a failure, and a
cancellation arriving between the legs of a simulated trade is re-raised as a cancellation
rather than being rewritten into a spurious execution error.

**Watchdog thresholds.** Refresh and scan pause after 3 consecutive failures and stop the
run at 12. An execution failure escalates straight to a stop, without pausing: pausing
execution because execution failed suppresses the only thing that could clear the streak.
A failed accounting write is an execution failure, so it ends the run — which is the
intended behaviour, because a run that cannot store its own accounting has nothing left to
observe.

**Restarting.** Stop with Ctrl-C, then re-run the same command against the same database.
Restart is safe and is exercised in the acceptance exercise: cash is replayed from the
ledger, holds left by the dead process are reaped, and interrupted attempts come back as
open exposure. Restarting does **not** re-run settlement; see §6.

## 6. Settle, then reconcile

Settlement is operator-driven on purpose. An automatic outcome feed that is wrong, late, or
disagreeing between venues would silently rewrite the trial's own P&L.

```bash
.venv/bin/event-arb settle --list
.venv/bin/event-arb settle \
    --trade-id <id> \
    --kalshi-outcome YES \
    --recorded-by "<your name>" \
    --evidence "<settlement authority and document you read>" \
    --dry-run
```

Check the printed `outcome alignment` line before dropping `--dry-run`. It says whether the
inversion came from the reviewed pair or from you. If the pair cannot be found the command
**refuses** rather than assuming alignment; pass `--polymarket-outcome YES|NO` to state each
venue explicitly, which is also how you record a genuine cross-venue discrepancy — two
markets that did not settle as equivalents, i.e. the case where a hedged position was not.

Repeating the command is safe: the credit is keyed by trade and account, so a second run
credits nothing and says `Already settled`.

Then reconcile, from a fresh process, against storage only:

```bash
set +e
.venv/bin/event-arb reconcile --json "$EVIDENCE/reconciliation-final.json" \
    2>&1 | tee "$EVIDENCE/06-reconcile.txt"
RECONCILE_STATUS=${PIPESTATUS[0]}      # not $?, which is tee's
set -e
echo "reconcile exit: $RECONCILE_STATUS" >> "$EVIDENCE/06-reconcile.txt"
```

(`scripts/run-paper-trial.sh` already did this in §5; repeat it here only after settling.)

It answers **two** independent questions, and needs both.

*Does each account's arithmetic close?*

    opening balance + every durable cash entry == ending balance

Per account, never in aggregate, because a USD gain and a USDC loss net to something
meaningless and a debit landing in the wrong account is what a total hides. Note that
Kalshi USD and Polymarket US USD are both `USD` and are **not** the same money.

*Is each retained trade's cash actually in the ledger?*

The arithmetic above cannot answer this, because both of its sides are sums of the same
ledger: delete a trade's `trade_debit` rows and they leave both sides together, so the
identity still closes while the cash the trade spent has quietly reappeared. Reproduced on
a disposable database — one trade retained, its two debits deleted, Kalshi cash back up
from 9951.2500 to 9998.2500 — and the old report called that balanced.

So every retained trade's implied entries are now re-derived from the **trade rows**, which
are stored separately from the ledger, and compared against what is there. Missing, wrong
amount, wrong account, unexpected and orphaned movements are each named. Retention
compaction is not corruption: the carry-forward entries name their own cutoff, so a trade
below that boundary is reported as *compacted and not verified* rather than as missing —
"not verified" and "verified sound" are different claims and the report keeps them apart.

`reconcile` is now genuinely read-only. It used to call the restoring path, which reaps
holds left by a dead process **and persists the release** — so inspecting a database
mutated it, and inspecting one while a trial was running released the reservations that
trial was using. Recovery and reporting are separate operations now; only `trial` recovers.

It exits non-zero when an account does not close, when a trade's cash is missing, when a
stored trade could not be rebuilt, when the run recorded an attempt storage never accepted,
or when an earlier run against this database died without shutting down. **A non-zero exit
means the run's figures are not evidence,** whatever the hedge rate above them says.

### If it reports an unfinished run

A process killed outright — a signal it never handles, a host going away — leaves storage
looking exactly like a clean shutdown, because a clean shutdown is also just "the writes
that happened, happened". So a run opens a marker in the journal beside the database and
only an orderly shutdown closes it. A start with no finish is the sole trace such a death
leaves, and it fails the evidence gate closed.

Clearing it is a claim that you looked, and is recorded as one:

```bash
.venv/bin/event-arb acknowledge-run <run-id> \
    --by "<your name>" \
    --note "<what you found>"
```

Both flags are required. The acknowledgement is **appended** — the crash record itself is
never removed, so the gate going green always leaves a named person and a stated reason
behind rather than a gap where a failure used to be. Do this only after reading
`02-trial.log` and the reconciliation, and record what you concluded in `$EVIDENCE/pair.md`.

## 7. Reporting the run honestly

Copy the trial summary, the daily report and the reconciliation into
`$EVIDENCE/report.md`, and state, in this order:

1. **Whether the accounts reconcile.** Nothing else in the report means anything until
   this is yes.
2. **Whether any attempt was invalidated.** A non-zero `paper_trades_invalidated` means the
   execution record has a hole: those attempts are in no count, no balance and no rate.
   This now survives a restart — the invalidation is written to the journal beside the
   database, not into the database that refused the write — so a fresh process reports the
   same hole rather than upgrading the run to complete.
3. **Whether any earlier run against this database is unfinished**, and whether you
   acknowledged it and why. An unfinished run is not a failed trade; it is a run whose
   attempts are unknown.
4. **Whether trade/ledger integrity is sound.** `reconcile` prints this separately from the
   per-account arithmetic because they answer different questions; a sound arithmetic with
   a failed integrity check means cash moved that no retained trade explains.
5. **Whether the feed was healthy for the whole run.** `refreshes_incomplete` above zero
   means some cycles had no usable book for a required market, so the scans that would have
   happened did not. `feed_was_healthy: false` in the trial summary is an outage, and an
   outage must never be reported as a quiet market.
6. **How many trades actually happened.** A no-trade run is a real and reportable result:
   it validates that ingestion, detection, the capital gate, the watchdog and persistence
   ran unattended for the duration. It validates **nothing** about fills, hedge quality or
   settlement, and must not be summarised as a successful trial.
7. **The evidence grade of every trade.** `measured` means leg 2 priced against a book that
   was re-fetched *and had moved*. `unchanged_reread` means the re-fetch happened and the
   book was identical — which shows the book did not move in that sample, not that hedging
   survives movement. `modelled` means a configured adverse-movement assumption produced
   the number, so the result describes the assumption. The daily report prints
   `evidence_caveat` for exactly this; quote it verbatim rather than paraphrasing.
8. **Whether settlement actually occurred.** If the market had not resolved by the end of
   the run, settlement is *pending* — say so rather than reporting the execution-time
   estimate as realised P&L. They are different numbers over different sets of trades.
9. **Every stop cause**, from `stopped_reason` and `last_error`.

## 8. Exit criteria for extending to 24–72 hours

All of the following, from the supervised run, reviewed by the operator:

- `reconcile` exits 0 and `paper_trades_invalidated` is 0;
- trade/ledger integrity is `sound`, with no findings;
- no unfinished run is outstanding, or every one is acknowledged with a stated reason;
- `refreshes_incomplete` is 0 and the summary reports `feed_was_healthy: true`;
- no watchdog stop that was not deliberately induced;
- the request budget held, with no venue rate-limit rejections in the log;
- the observed refresh batch is at or below the estimate `doctor` used, so the freshness
  bound was satisfied by measurement rather than by assumption;
- the pair's approval is still live — not suspended by a rulebook change mid-run;
- if trades occurred, at least one settled, reconciled, and its payout matched what the
  rulebooks said it should.

A longer run is a new authorization, not a continuation of this one.

## Appendix A — the offline acceptance exercise

The unattended-behaviour evidence this runbook rests on comes from two files, both
driving the real `TrialRunner` over fixture data with fresh temporary storage.

`tests/acceptance/test_offline_trial_acceptance.py` — **legacy Polymarket Global**
coverage, one injected fault per scenario: a dead feed, a failing scan, storage that
refuses a trade write, capital depletion, an interruption between the legs, an abrupt stop
with the interruption handler disabled, a settlement write that fails and is repaired
across a restart, and an uninjected control run.

`tests/acceptance/test_us_trial_acceptance.py` — **the deployment target**: Kalshi plus
Polymarket US, USD on both legs. Fresh schema migration, the recorded approval surviving a
restart, the startup preflight, a healthy end-to-end run, funded execution debiting the two
USD accounts separately, an interruption surviving a restart, settlement with **aligned**
and with **inverted** outcomes, an adapter-level feed outage, and a run that ended without
a clean shutdown. Keep the Global file — it is legacy coverage, not the proof for a trial
that will not use Global.

Both reconcile every account from storage afterwards, from a fresh process, using the
read-only inspection path.

```bash
EVENT_ARB_ACCEPTANCE_OUT=evidence/offline-acceptance \
    .venv/bin/python -m pytest tests/acceptance -p no:cacheprovider
```

19 scenarios; one `.json` and one `.txt` reconciliation each, prefixed `us-` for the US
target. Re-run it after any change to the accounting path, and attach its output alongside
a live run's.

**A real process kill is now tested.** `tests/integration/test_abrupt_termination.py`
spawns an actual subprocess, lets it open a run and take on real exposure, sends it
`SIGKILL`, and then asks a fresh process what it can still tell. No handler runs, no
`finally` executes, nothing flushes. The journal marker survives, the run is reported as
unfinished, the leg-1 checkpoint is intact, and the evidence gate fails closed.

**What remains untested.** The kill test runs on this host's filesystem, so it exercises
one operating system's behaviour rather than the general case; SQLite's durability under a
power loss or a filesystem that lies about `fsync` is not covered by anything here, and
should not be claimed. The Global abrupt-stop scenario still simulates the process boundary
rather than crossing it — the US and journal coverage crosses it instead. And fixture books
cannot move, so every leg 2 in both files is graded `unchanged_reread`: these scenarios show
the accounting held, never that hedging would.

## Appendix B — what this trial deliberately does not do

- **No WebSocket.** Kalshi signs its handshake even for public channels, and Polymarket US
  streaming requires the authenticated API. REST-only keeps the trial credential-free; the
  preflight refuses `websocket` with `polymarket.product=us` rather than starting a stream
  that cannot authenticate.
- **No Polymarket Global.** US only, and the settings refuse Global live access without an
  explicit operator eligibility confirmation.
- **No real orders.** Structurally, not by configuration.
- **No automated approval, and no widening of discovery.** If no equivalent pair exists,
  that is the finding — report it rather than loosening matching to produce one.
