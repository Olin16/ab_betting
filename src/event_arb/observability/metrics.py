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
    "paper_trades",
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
venue_up = Gauge(
    "event_arb_venue_up",
    "1 when the venue adapter is healthy, 0 otherwise.",
    ["venue"],
    registry=REGISTRY,
)


def render_metrics() -> bytes:
    """Render the registry in Prometheus text exposition format."""
    return generate_latest(REGISTRY)
