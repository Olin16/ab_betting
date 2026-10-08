"""The launch script must report the trial's exit code, not tee's.

The runbook's recorded exit code came from `echo "exit: $?"` after a pipeline ending in
`tee`. `$?` there is tee's status, and tee succeeds whenever it can write the file — so a
trial that stopped on a watchdog failure and exited 1 wrote "exit: 0" into its own
evidence. The operator's only record of the run said it finished cleanly.

These drive the real script with a stub `event-arb` so the exit-code plumbing is tested
without running a trial. No venue is contacted and no real database is touched.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "run-paper-trial.sh"

#: A stand-in for the `event-arb` entrypoint. `trial` exits with whatever TRIAL_EXIT says
#: and `reconcile` with RECONCILE_EXIT, so the script's propagation is what is under test.
STUB = """#!/usr/bin/env bash
case "$1" in
  manifest)   echo "stub manifest"; [[ "$2" == "--json" ]] && echo '{}' > "$3"; exit 0 ;;
  doctor)     echo "stub doctor"; exit 0 ;;
  trial)      echo "stub trial ran"; exit "${TRIAL_EXIT:-0}" ;;
  reconcile)  echo "stub reconcile ran"; exit "${RECONCILE_EXIT:-0}" ;;
  *)          echo "unexpected: $*" >&2; exit 99 ;;
esac
"""


@pytest.fixture
def stub_bin(tmp_path: Path) -> Path:
    path = tmp_path / "event-arb-stub"
    path.write_text(STUB)
    path.chmod(0o755)
    return path


def run(
    stub: Path, evidence: Path, *, trial_exit: int = 0, reconcile_exit: int = 0
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "EVENT_ARB_BIN": str(stub),
        "EVENT_ARB_TRIAL_ENV": "/nonexistent/trial.env",
        "TRIAL_DURATION": "1",
        "TRIAL_EXIT": str(trial_exit),
        "RECONCILE_EXIT": str(reconcile_exit),
    }
    return subprocess.run(
        ["bash", str(SCRIPT), str(evidence)],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO),
        timeout=120,
    )


def test_a_failing_trial_fails_the_script(stub_bin: Path, tmp_path: Path) -> None:
    """The defect: this used to exit 0 because tee succeeded."""
    evidence = tmp_path / "evidence"
    result = run(stub_bin, evidence, trial_exit=1)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "TRIAL DID NOT PRODUCE USABLE EVIDENCE" in result.stderr


def test_the_recorded_exit_code_is_the_trials_own(stub_bin: Path, tmp_path: Path) -> None:
    """And the evidence on disk has to say so too, not just the process status."""
    evidence = tmp_path / "evidence"
    run(stub_bin, evidence, trial_exit=7)

    assert "trial exit: 7" in (evidence / "02-trial.log").read_text()
    assert "trial_exit_code=7" in (evidence / "05-result.txt").read_text()


def test_a_failing_reconcile_alone_still_fails_the_script(stub_bin: Path, tmp_path: Path) -> None:
    """Books that do not close are not a successful run, however healthy the loops were."""
    evidence = tmp_path / "evidence"
    result = run(stub_bin, evidence, trial_exit=0, reconcile_exit=1)

    assert result.returncode == 1
    assert "reconcile_exit_code=1" in (evidence / "05-result.txt").read_text()


def test_a_clean_run_exits_zero_and_leaves_every_artifact(stub_bin: Path, tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    result = run(stub_bin, evidence)

    assert result.returncode == 0, result.stdout + result.stderr
    for name in (
        "00-manifest.json",
        "00-manifest.txt",
        "01-doctor.txt",
        "02-trial.log",
        "04-reconcile.txt",
        "05-result.txt",
    ):
        assert (evidence / name).exists(), f"{name} is missing from the evidence"
    assert "trial_exit_code=0" in (evidence / "05-result.txt").read_text()


def test_it_refuses_to_overwrite_existing_evidence(stub_bin: Path, tmp_path: Path) -> None:
    """Two runs written into one directory become one unreadable record."""
    evidence = tmp_path / "evidence"
    assert run(stub_bin, evidence).returncode == 0

    second = run(stub_bin, evidence)
    assert second.returncode == 2
    assert "refusing to overwrite" in second.stderr


def test_it_refuses_without_an_evidence_directory(stub_bin: Path) -> None:
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        env={**os.environ, "EVENT_ARB_BIN": str(stub_bin)},
        cwd=str(REPO),
        timeout=60,
    )
    assert result.returncode == 2
    assert "usage:" in result.stderr


def test_the_script_is_executable_and_syntactically_valid() -> None:
    assert os.access(SCRIPT, os.X_OK), "the runbook tells an operator to run this directly"
    check = subprocess.run(
        ["bash", "-n", str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert check.returncode == 0, check.stderr
