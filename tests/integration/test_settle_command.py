"""The `event-arb settle` command itself, not just the settlement helpers underneath it.

Outcome helpers were covered and the command was not, which is where the defect lived: the
command looked its pair up in `scanner.state.pairs`, a map `settle` never populates because
it restores the portfolio and nothing else. The lookup missed every time, `outcomes_inverted`
fell back to False, and an inverted pair settled as though it were aligned — the wrong side
paid, with nothing in the output to say so.

So these drive the real argparse namespace through `_cmd_settle` against a real database and
assert on exit codes and credited cash, because that is the surface an operator uses.
"""

from __future__ import annotations

import argparse
from decimal import Decimal
from pathlib import Path

import pytest

from event_arb.config import (
    OpportunitySettings,
    PaperSettings,
    PolymarketProduct,
    PolymarketSettings,
    Settings,
)
from event_arb.domain.enums import MatchStatus
from event_arb.main import _cmd_settle
from event_arb.paper.accounts import AccountId
from event_arb.persistence.database import Database
from event_arb.persistence.schema import ensure_schema
from event_arb.scanner import Scanner

pytestmark = pytest.mark.anyio


def make_settings(fixture_dir: Path, url: str) -> Settings:
    return Settings(
        database_url=url,
        fixture_dir=fixture_dir,
        polymarket=PolymarketSettings(product=PolymarketProduct.US),
        paper=PaperSettings(leg1_latency_ms=0, leg2_latency_ms=0),
        opportunity=OpportunitySettings(
            min_net_profit_usd=Decimal("0.50"),
            min_return_pct=Decimal("0.5"),
            max_book_age_ms=60_000,
            max_quantity=Decimal("100"),
        ),
    )


def settle_args(**over: object) -> argparse.Namespace:
    """Exactly the namespace `main()` builds for `event-arb settle`."""
    defaults: dict[str, object] = {
        "trade_id": "",
        "list": False,
        "kalshi_outcome": "YES",
        "polymarket_outcome": "",
        "outcomes_inverted": False,
        "recorded_by": "operator",
        "evidence": "venue settlement notice",
        "dry_run": False,
    }
    return argparse.Namespace(**{**defaults, **over})


async def traded(settings: Settings, *, inverted: bool) -> str:
    """Open a paper trade against a stored pair with the reviewer's inversion recorded."""
    database = Database(settings.database_url)
    await ensure_schema(database)
    scanner = Scanner(settings, database=database)
    try:
        await scanner.discover()
        await scanner.restore()
        scanner.generate_pairs(min_score=Decimal("0.4"))
        for pair in scanner.state.pairs.values():
            # The reviewer's recorded statement about the two markets, which is what the
            # command has to read back out of storage.
            scanner.state.pairs[pair.id] = pair.model_copy(
                update={"outcomes_inverted": inverted, "status": MatchStatus.APPROVED}
            )
        await scanner.persist_markets()
        await scanner.persist_pairs()
        await scanner.refresh_books()
        trade = await scanner.paper_trade_if_funded(scanner.scan().all_detected[0])
        assert trade is not None
        return trade.id
    finally:
        await scanner.close()


async def balances_now(settings: Settings) -> dict[str, str]:
    database = Database(settings.database_url)
    scanner = Scanner(settings, database=database)
    try:
        await scanner.restore_portfolio()
        return {
            a.key: str(scanner.portfolio.balance(a))
            for a in (AccountId.parse("kalshi:USD"), AccountId.parse("polymarket_us:USD"))
        }
    finally:
        await scanner.close()


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{tmp_path / 'settle.db'}"


async def test_the_command_reads_inversion_from_the_stored_pair(
    fixture_dir: Path, db_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """An inverted pair pays the opposite side on Polymarket, without being told twice.

    The operator states one outcome — the event's — and the reviewer's recorded inversion
    is what turns it into a per-venue pair. Reading that from storage is the whole fix.
    """
    settings = make_settings(fixture_dir, db_url)
    trade_id = await traded(settings, inverted=True)

    code = await _cmd_settle(settings, settle_args(trade_id=trade_id, kalshi_outcome="YES"))
    out = capsys.readouterr().out

    assert code == 0
    assert "outcome alignment   : inverted" in out
    assert "from the reviewed pair" in out
    assert "Kalshi resolves     : YES" in out
    assert "Polymarket resolves : NO" in out, "inverted means the other side won there"


async def test_an_aligned_stored_pair_settles_both_venues_the_same_way(
    fixture_dir: Path, db_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """The control for the test above: nothing is inverted unless the pair says so."""
    settings = make_settings(fixture_dir, db_url)
    trade_id = await traded(settings, inverted=False)

    code = await _cmd_settle(settings, settle_args(trade_id=trade_id, kalshi_outcome="YES"))
    out = capsys.readouterr().out

    assert code == 0
    assert "outcome alignment   : aligned" in out
    assert "Kalshi resolves     : YES" in out
    assert "Polymarket resolves : YES" in out


async def test_the_command_refuses_a_trade_whose_pair_it_cannot_find(
    fixture_dir: Path, db_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """No stored pair, no stated inversion: alignment is unknown, so it must not guess.

    Assuming alignment is exactly how an inverted pair gets paid the wrong way round, and
    the wrongness is invisible — a plausible number lands in a plausible account.
    """
    settings = make_settings(fixture_dir, db_url)
    trade_id = await traded(settings, inverted=True)

    database = Database(settings.database_url)
    scanner = Scanner(settings, database=database)
    try:
        await scanner.restore_portfolio()
        known = next(t for t in scanner.portfolio.trades if t.id == trade_id)
        orphaned = known.pair_id
    finally:
        await scanner.close()

    # Drop the pair the trade points at, standing in for a database restored without it.
    from sqlalchemy import delete

    from event_arb.persistence.models import MarketPairRow

    database = Database(settings.database_url)
    try:
        async with database.session() as session:
            await session.execute(delete(MarketPairRow).where(MarketPairRow.id == orphaned))
    finally:
        await database.dispose()

    before = await balances_now(settings)
    code = await _cmd_settle(settings, settle_args(trade_id=trade_id))
    out = capsys.readouterr().out

    assert code == 1
    assert "CANNOT ESTABLISH OUTCOME ALIGNMENT" in out
    assert "--polymarket-outcome" in out, "the refusal has to say how to proceed"
    assert await balances_now(settings) == before, "a refusal credits nothing"


async def test_an_operator_may_state_the_inversion_when_the_pair_is_gone(
    fixture_dir: Path, db_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--outcomes-inverted` is the documented way past the refusal, and it is attributed."""
    settings = make_settings(fixture_dir, db_url)
    trade_id = await traded(settings, inverted=False)

    code = await _cmd_settle(
        settings, settle_args(trade_id=trade_id, outcomes_inverted=True, kalshi_outcome="NO")
    )
    out = capsys.readouterr().out

    assert code == 0
    assert "outcome alignment   : inverted (operator-stated)" in out
    assert "Kalshi resolves     : NO" in out
    assert "Polymarket resolves : YES" in out


async def test_explicit_per_venue_outcomes_record_a_genuine_discrepancy(
    fixture_dir: Path, db_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Both venues can resolve the same way on an *inverted* pair. That is a real event.

    It means the two markets did not settle as equivalents — the case a hedged position
    stops being hedged — so the command has to record what each venue actually did rather
    than derive one from the other.
    """
    settings = make_settings(fixture_dir, db_url)
    trade_id = await traded(settings, inverted=True)

    code = await _cmd_settle(
        settings,
        settle_args(trade_id=trade_id, kalshi_outcome="YES", polymarket_outcome="YES"),
    )
    out = capsys.readouterr().out

    assert code == 0
    assert "outcome alignment" not in out, "nothing is derived when both are stated"
    assert "Kalshi resolves     : YES" in out
    assert "Polymarket resolves : YES" in out


async def test_a_dry_run_writes_nothing(
    fixture_dir: Path, db_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = make_settings(fixture_dir, db_url)
    trade_id = await traded(settings, inverted=True)
    before = await balances_now(settings)

    code = await _cmd_settle(settings, settle_args(trade_id=trade_id, dry_run=True))
    out = capsys.readouterr().out

    assert code == 0
    assert "DRY RUN: nothing was written." in out
    assert await balances_now(settings) == before


async def test_the_command_refuses_an_unknown_trade_id(
    fixture_dir: Path, db_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = make_settings(fixture_dir, db_url)
    await traded(settings, inverted=False)

    code = await _cmd_settle(settings, settle_args(trade_id="no-such-trade"))
    out = capsys.readouterr().out

    assert code == 1
    assert "CANNOT ESTABLISH OUTCOME ALIGNMENT" in out


async def test_the_command_requires_attribution_and_evidence(
    fixture_dir: Path, db_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """A settlement nobody signed is not evidence, whatever it does to the balance."""
    settings = make_settings(fixture_dir, db_url)
    trade_id = await traded(settings, inverted=False)
    before = await balances_now(settings)

    assert await _cmd_settle(settings, settle_args(trade_id=trade_id, recorded_by=" ")) == 1
    assert "--recorded-by is required" in capsys.readouterr().out
    assert await _cmd_settle(settings, settle_args(trade_id=trade_id, evidence="")) == 1
    assert "--evidence is required" in capsys.readouterr().out
    assert await balances_now(settings) == before


async def test_listing_unsettled_trades_settles_nothing(
    fixture_dir: Path, db_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = make_settings(fixture_dir, db_url)
    trade_id = await traded(settings, inverted=False)
    before = await balances_now(settings)

    code = await _cmd_settle(settings, settle_args(list=True))
    out = capsys.readouterr().out

    assert code == 0
    assert trade_id in out
    assert "1 unsettled paper trade(s)" in out
    assert await balances_now(settings) == before


async def test_settling_the_same_trade_twice_credits_once(
    fixture_dir: Path, db_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """An operator unsure whether the first attempt landed repeats it. That must be safe."""
    settings = make_settings(fixture_dir, db_url)
    trade_id = await traded(settings, inverted=False)

    assert await _cmd_settle(settings, settle_args(trade_id=trade_id)) == 0
    capsys.readouterr()
    once = await balances_now(settings)

    assert await _cmd_settle(settings, settle_args(trade_id=trade_id)) == 0
    assert "Already settled" in capsys.readouterr().out
    assert await balances_now(settings) == once
