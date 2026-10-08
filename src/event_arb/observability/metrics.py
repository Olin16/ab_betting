"""Prometheus-compatible metrics.

A dedicated registry is used rather than the global default so that repeated imports in
tests do not raise duplicate-timeseries errors.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

__all__ = [
    "REGISTRY",
    "book_age_ms",
    "books_received",
    "opportunities_detected",
    "opportunities_rejected",
    "paper_refetches",
    "paper_settlements",
    "paper_trade_evidence",
    "paper_trades",
    "paper_trades_invalidated",
    "paper_trades_refused",
    "render_metrics",
    "resyncs",
    "venue_up",
    "ws_reconnects",
]

REGISTRY = CollectorRegistry()

books_received = Counter(
    "event_arb_books_received_total",
    "Order-book snapshots and deltas applied.",
    ["venue"],
    registry=REGISTRY,
)
book_age_ms = Histogram(
    "event_arb_book_age_ms",
    "Age of an order book at evaluation time (milliseconds).",
    ["venue"],
    buckets=(50, 100, 250, 500, 1000, 2000, 5000, 15000),
    registry=REGISTRY,
)
opportunities_detected = Counter(
    "event_arb_opportunities_detected_total",
    "Opportunities emitted, by label and direction.",
    ["label", "direction"],
    registry=REGISTRY,
)
opportunities_rejected = Counter(
    "event_arb_opportunities_rejected_total",
    "Candidate opportunities rejected, by reason.",
    ["reason"],
    registry=REGISTRY,
)
ws_reconnects = Counter(
    "event_arb_ws_reconnects_total",
    "WebSocket reconnect attempts.",
    ["venue"],
    registry=REGISTRY,
)
resyncs = Counter(
    "event_arb_resyncs_total",
    "Snapshot resynchronizations triggered by sequence gaps.",
    ["venue"],
    registry=REGISTRY,
)
paper_trades = Counter(
    "event_arb_paper_trades_total",
    "Simulated trades, by terminal status.",
    ["status"],
    registry=REGISTRY,
)
paper_refetches = Counter(
    "event_arb_paper_refetches_total",
    "Extra order-book requests issued by paper execution at leg arrival time. "
    "These are additional to scan traffic and count against the venue rate limit.",
    ["venue"],
    registry=REGISTRY,
)
paper_trade_evidence = Counter(
    "event_arb_paper_trade_evidence_total",
    "Simulated trades by how much their leg-2 book is worth as evidence: measured, "
    "unchanged_reread, or modelled. Only 'measured' supports a hedge-rate claim.",
    ["grade"],
    registry=REGISTRY,
)
paper_trades_refused = Counter(
    "event_arb_paper_trades_refused_total",
    "Detected opportunities the paper capital gate refused, by reason. A trial with a "
    "rising count here is not observing a quiet market; it is out of simulated funds or "
    "at a configured limit.",
    ["reason"],
    registry=REGISTRY,
)
paper_trades_invalidated = Counter(
    "event_arb_paper_trades_invalidated_total",
    "Simulated attempts whose accounting storage refused, by the stage that failed. "
    "These were never published to the portfolio, so they appear in no other count — "
    "any value above zero means the run's execution record has a stated hole in it.",
    ["stage"],
    registry=REGISTRY,
)
paper_settlements = Counter(
    "event_arb_paper_settlements_total",
    "Paper trades whose settlement was recorded, by whether the hedge held.",
    ["hedge_held"],
    registry=REGISTRY,
)
venue_up = Gauge(
    "event_arb_venue_up",
    "1 when the venue adapter is healthy, 0 otherwise.",
    ["venue"],
    registry=REGISTRY,
)


def render_metrics() -> bytes:
    """Render the registry in Prometheus text exposition format."""
    return generate_latest(REGISTRY)
