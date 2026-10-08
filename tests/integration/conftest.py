"""Fixtures shared across the integration suite.

``db_url`` lives here rather than in one test module because several modules need the
same disposable database and importing a fixture between modules makes every use of it
look like a redefinition to the linter.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    """A file-backed SQLite database for one test.

    Deliberately file-backed rather than in-memory: these tests reopen the same database
    from a second "process", and they exercise the run journal, which is a sidecar file
    beside the database and has nothing to sit beside when the database is in memory.
    """
    return f"sqlite+aiosqlite:///{tmp_path / 'durability.db'}"
