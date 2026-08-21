# Threat Model

Scope: this application, its credentials, its logs, and its dependencies. Assets are
ranked by what an attacker gains, and the ranking is not close — key material first.

## Assets

| Asset | Impact if compromised | Where it lives |
| --- | --- | --- |
| **Wallet private key** | Irreversible, total loss of on-chain funds. No recourse. | Never in this repo. Behind the `Signer` interface only. |
| **Kalshi API private key** | Account takeover: place and cancel orders, read positions. | A file path in config; key bytes read at use time only. |
| **API key IDs** | Enables targeted attacks; not sufficient alone. | `.env` (untracked) |
| **Webhook URLs** | Alert spoofing or leakage of trading intent. | `.env` (untracked) |
| **Approved-pair data** | Corruption causes unhedged positions believed to be hedged. | Database |
| **Order books / opportunities** | Low sensitivity; reveals strategy if published. | Database |

## T1 — Credential exfiltration through the repository

*Threat:* a key, `.env`, or PEM file is committed and reaches a remote.

Controls:
- `.gitignore` blocks `.env`, `*.pem`, `*.key`, `*.p8`, `wallet*.json`, `secrets/`, and
  `*private*key*` patterns.
- `.env.example` contains placeholders only; there is no real value to accidentally copy.
- CI `secret-scan` job fails the build on any tracked credential-shaped file or any
  `BEGIN ... PRIVATE KEY` block anywhere in the tree.
- Private key material is never loaded into a config object or a Pydantic model — only a
  *path* is configured, and bytes are read at signing time.

*Residual risk:* a developer can still `git add -f`. Enable pre-commit secret scanning
(gitleaks or similar) before this project handles a real key.

## T2 — Credential leakage through logs

*Threat:* a key, signature, or auth header is written to stdout, a log aggregator, or an
error tracker, where it is retained and widely readable.

Controls:
- Redaction is a **structlog processor** that sees every event
  (`observability/logging.py`), not a call-site convention. Call-site discipline fails
  exactly once and then the key is in a log file forever.
- Keys are redacted by name (substring match over `key`, `secret`, `token`, `signature`,
  `authorization`, `wallet`, `mnemonic`, `seed`, and more), case-insensitively.
- Values are redacted by *shape* too: PEM blocks, long base64/hex blobs, and `0x`-prefixed
  hex strings are stripped even under an innocuous key name.
- Redaction recurses through nested dicts, lists, and tuples.
- No signed request or raw auth header is ever logged.
- The Polymarket geoblock response includes the caller's IP; it is deliberately not
  stored or logged.

Tested in `tests/unit/test_logging_redaction.py`.

## T3 — Credential leakage through outbound webhooks

*Threat:* an alert payload sent to a third-party endpoint contains secrets or raw API
metadata.

Controls: `alerts.py` builds payloads from an explicit **allowlist** of fields. Nothing is
serialized wholesale, so a new upstream field cannot silently start leaving the process.
Tested by asserting the serialized payload contains no credential-shaped keys.

## T4 — Unauthorized or accidental trading

*Threat:* the system places a real order — through a bug, a misconfiguration, or a
compromised process.

Controls (defence in depth):
1. **No implementation exists.** Phase 7 was not built. The only `ExecutionAdapter` raises
   `LiveTradingDisabledError` on every method.
2. `trading_enabled` defaults to `False`, and `Settings.live_trading_possible` returns
   `False` unconditionally — setting the flag changes nothing.
3. Read-only venue clients have **no order-entry methods at all**, not even disabled ones.
   A test asserts this structurally.
4. `ExecutionCoordinator` defaults to the disabled adapter, so a caller who forgets to
   pass one gets the safe behaviour.
5. `TradingControls` would independently deny on kill switch, circuit breaker, geoblock,
   unapproved pair, size, exposure, daily loss, and minimum balance.

Tested in `tests/unit/test_execution_disabled.py`, including that all controls passing is
*still* not enough to trade.

## T5 — Compliance bypass

*Threat:* orders are submitted from a restricted jurisdiction.

Controls: `GeoblockChecker` calls the official endpoint and **fails closed**. A network
error, timeout, non-200, non-JSON body, missing field, or non-boolean `blocked` all
resolve to "blocked". Only an explicit `blocked: false` permits anything. Blocked results
are never cached as allowed. The system never attempts to circumvent a restriction, and
contains no proxy, VPN, or IP-rotation capability.

## T6 — Data poisoning / malformed venue responses

*Threat:* a malformed or hostile API response corrupts a book, manufacturing a spread.

Controls: mappers validate structure and raise `MarketDataError` rather than skipping bad
entries silently; a book built from nothing is marked `is_complete=False`; sequence gaps
force resync and mark books stale; stale or incomplete books are rejected outright by the
opportunity engine. Failure mode is a missed opportunity, never a fabricated one.

## T7 — Dependency compromise

*Threat:* a malicious or compromised package exfiltrates credentials at import time.

Controls: a small, mainstream dependency set; version floors in `pyproject.toml`; CI runs
on a clean checkout; the container runs as a non-root user (uid 10001).

*Residual risk:* no lockfile with hashes is committed, and no SBOM or vulnerability scan
runs. **Before handling real credentials**, add `uv.lock` with hashes, enable Dependabot,
and add `pip-audit` to CI.

## T8 — Local privilege / host compromise

*Threat:* an attacker with local access reads `.env` or the key file.

Controls: none in-application. Store keys with `0600` permissions outside the repo, prefer
an OS keychain or secrets manager, and treat any host that holds a wallet key as
production infrastructure.

## Explicitly out of scope

- Dashboard authentication. It binds to `127.0.0.1` by default and has **no auth**. Do not
  expose it to a network without putting an authenticating proxy in front.
- Multi-tenant isolation, rate limiting of the dashboard, and CSRF protection on the JSON
  API (the approval endpoint is state-changing and unauthenticated).
- Venue-side compromise and settlement disputes.
