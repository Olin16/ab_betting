#!/usr/bin/env bash
#
# Run one supervised paper trial and keep its evidence, with the trial's own exit code.
#
# The runbook used to say:
#
#     event-arb trial ... 2>&1 | tee "$EVIDENCE/01-trial.log"
#     echo "exit: $?" | tee -a "$EVIDENCE/01-trial.log"
#
# `$?` after a pipeline is the exit status of the *last* command in it, which is `tee`.
# tee succeeds whenever it can write the file, so a trial that stopped on a watchdog
# failure and exited 1 recorded "exit: 0" in its own evidence — and the operator's only
# record of the run said it finished cleanly. This script reads PIPESTATUS instead, and
# exits with the trial's status so a caller or CI sees the real result.
#
# Usage:
#   scripts/run-paper-trial.sh EVIDENCE_DIR [-- extra event-arb trial args...]
#
# Environment:
#   EVENT_ARB_TRIAL_ENV   config file to source (default: config/trial.env)
#   EVENT_ARB_BIN         event-arb entrypoint  (default: .venv/bin/event-arb)
#   TRIAL_DURATION        seconds to run        (default: 2700, i.e. 45 minutes)

set -euo pipefail

EVIDENCE="${1:-}"
if [[ -z "$EVIDENCE" ]]; then
  echo "usage: $0 EVIDENCE_DIR [-- extra trial args...]" >&2
  exit 2
fi
shift
[[ "${1:-}" == "--" ]] && shift

BIN="${EVENT_ARB_BIN:-.venv/bin/event-arb}"
TRIAL_ENV="${EVENT_ARB_TRIAL_ENV:-config/trial.env}"
DURATION="${TRIAL_DURATION:-2700}"

# Refuse to write into a directory that already holds evidence. Overwriting a previous
# run's artifacts in place is how two runs become one unreadable record.
if [[ -e "$EVIDENCE" ]] && [[ -n "$(ls -A "$EVIDENCE" 2>/dev/null)" ]]; then
  echo "refusing to overwrite existing evidence in $EVIDENCE" >&2
  exit 2
fi
mkdir -p "$EVIDENCE"

if [[ -f "$TRIAL_ENV" ]]; then
  # `set -a` exports everything the file defines without printing any of it.
  set -a
  # shellcheck disable=SC1090
  . "$TRIAL_ENV"
  set +a
else
  echo "note: $TRIAL_ENV not found; running with the ambient environment" >&2
fi

# What code produced this evidence. By content, including untracked files: most of this
# implementation is uncommitted, so a commit hash alone does not describe it.
"$BIN" manifest --json "$EVIDENCE/00-manifest.json" > "$EVIDENCE/00-manifest.txt"

"$BIN" doctor > "$EVIDENCE/01-doctor.txt" 2>&1
echo "doctor exit: $?" >> "$EVIDENCE/01-doctor.txt"

set +e
"$BIN" trial \
  --duration "$DURATION" \
  --reconciliation-json "$EVIDENCE/03-reconciliation.json" \
  "$@" 2>&1 | tee "$EVIDENCE/02-trial.log"
# PIPESTATUS[0] is the trial's status; $? at this point would be tee's.
TRIAL_STATUS=${PIPESTATUS[0]}
set -e

echo "trial exit: $TRIAL_STATUS" | tee -a "$EVIDENCE/02-trial.log"

# Reconcile from storage in a fresh process, which is the only view that proves anything
# durable. Read-only: it will not release holds or otherwise touch the database.
set +e
"$BIN" reconcile --json "$EVIDENCE/04-reconcile.json" 2>&1 | tee "$EVIDENCE/04-reconcile.txt"
RECONCILE_STATUS=${PIPESTATUS[0]}
set -e
echo "reconcile exit: $RECONCILE_STATUS" | tee -a "$EVIDENCE/04-reconcile.txt"

{
  echo "trial_exit_code=$TRIAL_STATUS"
  echo "reconcile_exit_code=$RECONCILE_STATUS"
  echo "evidence_dir=$EVIDENCE"
  echo "completed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > "$EVIDENCE/05-result.txt"

if [[ "$TRIAL_STATUS" -ne 0 ]] || [[ "$RECONCILE_STATUS" -ne 0 ]]; then
  echo "" >&2
  echo "TRIAL DID NOT PRODUCE USABLE EVIDENCE" >&2
  echo "  trial exit: $TRIAL_STATUS, reconcile exit: $RECONCILE_STATUS" >&2
  echo "  see $EVIDENCE/02-trial.log and $EVIDENCE/04-reconcile.txt" >&2
  # The trial's failure takes precedence; a nonzero reconcile alone still fails the run.
  [[ "$TRIAL_STATUS" -ne 0 ]] && exit "$TRIAL_STATUS"
  exit "$RECONCILE_STATUS"
fi

echo ""
echo "trial complete; evidence in $EVIDENCE"
