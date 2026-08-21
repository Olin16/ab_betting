"""Shared test fixtures.

Every test in this suite runs against recorded fixtures. No test may contact a live
venue API or place an order of any kind; the ``live`` marker gates the only optional
read-only exception and is deselected by default.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from event_arb.config import Settings
from event_arb.domain.enums import Venue
from event_arb.venues.fixture import FixtureAdapter

FIXTURE_DIR = Path(__file__).parent / "fixtures"

KALSHI_FED = "KXFEDDECISION-26SEP-CUT25"
KALSHI_NYC = "KXHIGHNY-26AUG22-B90"
KALSHI_CPI = "KXCPIYOY-26SEP-T3.0"
POLY_FED = "0xfed26sepcut25aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
POLY_NYC = "0xnychigh26aug22bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
POLY_CPI = "0xcpiyoy26augcccccccccccccccccccccccccccccccccccccccccccccccccc"


@pytest.fixture
def fixture_dir() -> Path:
    return FIXTURE_DIR


@pytest.fixture
def kalshi_adapter() -> FixtureAdapter:
    return FixtureAdapter(Venue.KALSHI, FIXTURE_DIR)


@pytest.fixture
def polymarket_adapter() -> FixtureAdapter:
    return FixtureAdapter(Venue.POLYMARKET, FIXTURE_DIR)


@pytest.fixture
def settings() -> Settings:
    return Settings(fixture_dir=FIXTURE_DIR)
