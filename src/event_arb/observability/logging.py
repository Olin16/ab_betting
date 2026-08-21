"""Structured JSON logging with mandatory secret redaction.

Redaction is implemented as a structlog processor rather than at call sites, because a
call-site discipline fails exactly once and then a private key is in a log file forever.
Anything whose key looks sensitive is replaced before rendering, and long base64/hex
blobs (signatures, keys) are truncated even when the key name is unknown.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any, Final

import structlog
from structlog.typing import EventDict, WrappedLogger

__all__ = ["configure_logging", "get_logger", "redact_processor", "redact_value"]

#: Substrings that mark a field as secret. Matched case-insensitively.
_SENSITIVE_KEY_PARTS: Final[tuple[str, ...]] = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "access_key",
    "private_key",
    "privatekey",
    "signature",
    "authorization",
    "auth",
    "credential",
    "cookie",
    "session",
    "mnemonic",
    "seed",
    "passphrase",
    "wallet",
    "sign",
    "nonce",
    "bearer",
)

REDACTED: Final = "[REDACTED]"

#: PEM blocks and long opaque blobs, redacted even inside otherwise-benign strings.
_PEM_RE: Final = re.compile(r"-----BEGIN [^-]+-----.*?-----END [^-]+-----", re.DOTALL)
_LONG_BLOB_RE: Final = re.compile(r"\b[A-Za-z0-9+/=_-]{40,}\b")
_HEX_KEY_RE: Final = re.compile(r"\b0x[0-9a-fA-F]{32,}\b")


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def redact_value(value: Any) -> Any:
    """Recursively redact secrets from an arbitrary log value."""
    if isinstance(value, str):
        scrubbed = _PEM_RE.sub(REDACTED, value)
        scrubbed = _HEX_KEY_RE.sub(REDACTED, scrubbed)
        return _LONG_BLOB_RE.sub(REDACTED, scrubbed)
    if isinstance(value, dict):
        return {
            k: (REDACTED if _is_sensitive_key(str(k)) else redact_value(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        rendered = [redact_value(v) for v in value]
        return type(value)(rendered) if isinstance(value, list) else tuple(rendered)
    return value


def redact_processor(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """structlog processor that strips secrets from every event."""
    for key in list(event_dict.keys()):
        if _is_sensitive_key(str(key)):
            event_dict[key] = REDACTED
        else:
            event_dict[key] = redact_value(event_dict[key])
    return event_dict


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    """Install the structlog pipeline. Safe to call more than once."""
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
        force=True,
    )
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if fmt == "json"
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            redact_processor,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
