"""No automated path may set ``manually_approved``.

``--auto-approve`` used to run the rule comparison and, when it found no material
difference, write ``manually_approved = True`` — after which opportunities on that pair
were labelled CONFIRMED. It worked against live data too. Automated text comparison cannot
establish settlement equivalence; only a human who read both rulebooks can.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from event_arb.config import (
    DataSource,
    PolymarketProduct,
    PolymarketSettings,
    Settings,
)
from event_arb.demo_reviews import DemoReviewError, load_recorded_reviews

REPO = Path(__file__).resolve().parents[2]


def test_the_auto_approve_flag_no_longer_exists() -> None:
    """A removed flag that still parses is a removed flag that still runs."""
    for command in ("scan", "paper"):
        result = subprocess.run(
            [sys.executable, "-m", "event_arb.main", command, "--auto-approve"],
            capture_output=True,
            text=True,
            cwd=REPO,
        )
        assert result.returncode != 0
        assert "unrecognized arguments: --auto-approve" in result.stderr


def _sets_manual_approval_true(tree: ast.AST) -> bool:
    """Does this module set ``manually_approved`` to a literal True anywhere in code?

    AST rather than grep: the previous text search matched the word inside a docstring that
    explains why the flag was removed, which is exactly the sort of false positive that
    gets a guard deleted rather than fixed.
    """
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.keyword)
            and node.arg == "manually_approved"
            and isinstance(node.value, ast.Constant)
            and node.value.value is True
        ):
            return True
        if isinstance(node, ast.Assign):
            for target in node.targets:
                name = getattr(target, "attr", None) or getattr(target, "id", None)
                if (
                    name == "manually_approved"
                    and isinstance(node.value, ast.Constant)
                    and node.value.value is True
                ):
                    return True
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=False):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "manually_approved"
                    and isinstance(value, ast.Constant)
                    and value.value is True
                ):
                    return True
    return False


def test_only_the_approval_module_can_set_manual_approval() -> None:
    """One chokepoint. Anything else granting approval is a bypass of the human gate."""
    offenders = sorted(
        str(path.relative_to(REPO))
        for path in (REPO / "src").rglob("*.py")
        if _sets_manual_approval_true(ast.parse(path.read_text()))
    )
    assert offenders == ["src/event_arb/matching/approval.py"]


def test_recorded_reviews_refuse_to_load_against_live_data() -> None:
    settings = Settings(
        data_source=DataSource.LIVE,
        polymarket=PolymarketSettings(product=PolymarketProduct.US),
    )
    with pytest.raises(DemoReviewError, match="fixture-only"):
        load_recorded_reviews(settings)


def test_recorded_reviews_load_for_fixtures(fixture_dir: Path) -> None:
    settings = Settings(
        fixture_dir=fixture_dir,
        polymarket=PolymarketSettings(product=PolymarketProduct.US),
    )
    reviews = load_recorded_reviews(settings)
    assert reviews, "the fixture corpus should carry at least one recorded review"
    for review in reviews:
        assert review.reviewed_by, "a recorded review must name who made it"
        assert review.kalshi_source.evidence, "a recorded review must cite its evidence"
        assert review.polymarket_source.evidence


def test_a_recorded_review_names_exact_ids_and_cannot_generalise(fixture_dir: Path) -> None:
    """Matching is on identity, never on similarity — that is the whole difference."""
    settings = Settings(
        fixture_dir=fixture_dir,
        polymarket=PolymarketSettings(product=PolymarketProduct.US),
    )
    review = load_recorded_reviews(settings)[0]

    assert review.matches(
        review.kalshi_market_id, review.polymarket_market_id, review.polymarket_product
    )
    # A different market, however similar, is not covered.
    assert not review.matches(
        "SOME-OTHER-KALSHI-ID", review.polymarket_market_id, review.polymarket_product
    )
    assert not review.matches(
        review.kalshi_market_id, "some-other-poly-id", review.polymarket_product
    )
    # Nor the same ids on the other product.
    assert not review.matches(
        review.kalshi_market_id, review.polymarket_market_id, "polymarket_global"
    )
