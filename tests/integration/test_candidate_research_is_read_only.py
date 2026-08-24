"""Candidate research must never change a disposition a human recorded.

It used to call ``Scanner.restore()`` before discovering anything. restore re-verifies
every approval against markets fetched from the venue *this run*, and candidate research
fetches a bounded catalogue page rather than the specific markets an old approval points
at — so every approved pair looked unobserved and was suspended and persisted as such.
A read-only research command silently revoked human decisions.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from event_arb.config import Settings
from event_arb.domain.enums import MatchStatus
from event_arb.domain.models import MarketPair
from event_arb.persistence.database import Database
from event_arb.persistence.repositories import MarketPairRepository
from event_arb.scanner import Scanner

pytestmark = pytest.mark.anyio


@pytest.fixture
async def database() -> Database:
    """In-memory, per-test. Never the project database."""
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.create_all()
    return db


#: Every state a human can put a pair into. None may be altered by research.
HUMAN_DISPOSITIONS = [
    MatchStatus.APPROVED,
    MatchStatus.REJECTED,
    MatchStatus.SUSPENDED,
    MatchStatus.EXPERT_REVIEW,
    MatchStatus.UNDER_REVIEW,
]


def _pair(status: MatchStatus) -> MarketPair:
    return MarketPair(
        id="deadbeef" * 4,
        kalshi_market_id="KXFEDDECISION-26SEP-CUT25",
        polymarket_market_id="fed-cuts-25bps-september-2026",
        status=status,
        manually_approved=status is MatchStatus.APPROVED,
        semantic_score=Decimal("0.9"),
        reviewer_notes=f"human decision: {status.value}",
        approved_by="human@example.com" if status is MatchStatus.APPROVED else "",
    )


async def _stored(database: Database) -> dict[str, MarketPair]:
    async with database.session() as session:
        return await MarketPairRepository(session).load_all()


@pytest.mark.parametrize("status", HUMAN_DISPOSITIONS, ids=lambda s: s.value)
async def test_research_preserves_every_human_disposition_across_restart(
    us_settings: Settings, database: Database, status: MatchStatus
) -> None:
    settings = us_settings
    async with database.session() as session:
        await MarketPairRepository(session).save(_pair(status))
    before = (await _stored(database))["deadbeef" * 4]

    # A *fresh* Scanner: this is the restart. Research reads dispositions and writes only
    # pairs it has never seen.
    scanner = Scanner(settings, database=database)
    existing = await scanner.load_dispositions()
    assert existing["deadbeef" * 4].status is status

    await scanner.discover()
    scanner.generate_pairs(min_score=Decimal("0"))
    await scanner.persist_new_pairs(scanner.state.pairs.values())

    after = (await _stored(database))["deadbeef" * 4]
    assert after.status is before.status
    assert after.manually_approved is before.manually_approved
    assert after.reviewer_notes == before.reviewer_notes
    assert after.approved_by == before.approved_by


async def test_loading_dispositions_never_writes_anything(
    us_settings: Settings, database: Database
) -> None:
    """restore() persisted its suspensions; load_dispositions must not persist at all."""
    async with database.session() as session:
        await MarketPairRepository(session).save(_pair(MatchStatus.APPROVED))

    scanner = Scanner(us_settings, database=database)  # type: ignore[arg-type]
    loaded = await scanner.load_dispositions()

    # Mutating the returned copy must not reach storage.
    loaded["deadbeef" * 4] = loaded["deadbeef" * 4].model_copy(
        update={"status": MatchStatus.REJECTED, "manually_approved": False}
    )
    stored = (await _stored(database))["deadbeef" * 4]
    assert stored.status is MatchStatus.APPROVED
    assert stored.manually_approved is True


async def test_persist_new_pairs_will_not_overwrite_an_existing_row(
    us_settings: Settings, database: Database
) -> None:
    """The write path has no way to express "change an existing disposition"."""
    async with database.session() as session:
        await MarketPairRepository(session).save(_pair(MatchStatus.APPROVED))

    scanner = Scanner(us_settings, database=database)  # type: ignore[arg-type]
    hostile = _pair(MatchStatus.APPROVED).model_copy(
        update={
            "status": MatchStatus.SUSPENDED,
            "manually_approved": False,
            "reviewer_notes": "clobbered by research",
        }
    )
    written = await scanner.persist_new_pairs([hostile])

    assert written == 0
    stored = (await _stored(database))["deadbeef" * 4]
    assert stored.status is MatchStatus.APPROVED
    assert stored.manually_approved is True
    assert stored.reviewer_notes == "human decision: approved"


async def test_a_genuinely_new_pair_is_still_written(
    us_settings: Settings, database: Database
) -> None:
    """Skipping existing rows must not turn into skipping everything."""
    scanner = Scanner(us_settings, database=database)  # type: ignore[arg-type]
    written = await scanner.persist_new_pairs([_pair(MatchStatus.SUGGESTED)])
    assert written == 1
    assert (await _stored(database))["deadbeef" * 4].status is MatchStatus.SUGGESTED
