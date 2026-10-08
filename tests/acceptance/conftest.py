"""Where the acceptance exercise writes the evidence it produces.

By default it writes into pytest's temporary directory, so running the suite leaves
nothing behind. Point ``EVENT_ARB_ACCEPTANCE_OUT`` at a real directory to keep the
artifacts — that is how an operator produces the reconciliation evidence the trial
runbook asks for::

    EVENT_ARB_ACCEPTANCE_OUT=evidence/offline-acceptance \\
        .venv/bin/python -m pytest tests/acceptance -p no:cacheprovider
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def evidence_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    configured = os.environ.get("EVENT_ARB_ACCEPTANCE_OUT", "").strip()
    if configured:
        destination = Path(configured).resolve()
        destination.mkdir(parents=True, exist_ok=True)
        return destination
    return tmp_path_factory.mktemp("acceptance-evidence")


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    """A disposable, file-backed database for one acceptance scenario.

    File-backed because every scenario here reopens the database as a fresh process, and
    because the run journal is a sidecar file that has nothing to sit beside otherwise.
    """
    return f"sqlite+aiosqlite:///{tmp_path / 'acceptance.db'}"
