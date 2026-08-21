"""Kalshi request signing.

Kalshi authenticates with an RSA key pair: the client signs a message derived from the
request and sends the signature alongside the API key ID.

**Unverified detail** (see docs/api-assumptions.md A5): the exact message string Kalshi
expects — specifically whether the path includes the ``/trade-api/v2`` prefix and whether
the query string participates — was not confirmed from the documentation pages read for
this build. :func:`signature_message` isolates that decision in one place so it can be
corrected without touching the rest of the client.

Nothing here ever logs the private key, the signature, or the message.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from event_arb.domain.enums import Venue
from event_arb.venues.base import AuthenticationError

__all__ = ["KalshiSigner", "load_private_key", "signature_message"]


def signature_message(timestamp_ms: int, method: str, path: str) -> str:
    """Build the string that is signed.

    Path is used **without** the query string.
    """
    return f"{timestamp_ms}{method.upper()}{path}"


def load_private_key(path: Path) -> rsa.RSAPrivateKey:
    """Load an unencrypted PEM RSA private key from disk.

    The key is read directly from the filesystem and held only in memory. It is never
    placed in configuration objects, log records, or database rows.
    """
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise AuthenticationError(
            f"cannot read Kalshi private key at {path}", Venue.KALSHI
        ) from exc

    key = serialization.load_pem_private_key(data, password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise AuthenticationError("Kalshi private key must be an RSA key", Venue.KALSHI)
    return key


@dataclass
class KalshiSigner:
    """Produces the three Kalshi auth headers for a request."""

    api_key_id: str
    private_key: rsa.RSAPrivateKey

    @classmethod
    def from_path(cls, api_key_id: str, key_path: Path) -> KalshiSigner:
        return cls(api_key_id=api_key_id, private_key=load_private_key(key_path))

    def sign(self, timestamp_ms: int, method: str, path: str) -> str:
        """Return the base64 RSA-PSS/SHA-256 signature for a request."""
        message = signature_message(timestamp_ms, method, path).encode("utf-8")
        signature = self.private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("ascii")

    def headers(self, method: str, path: str, timestamp_ms: int | None = None) -> dict[str, str]:
        ts = timestamp_ms if timestamp_ms is not None else int(datetime.now(UTC).timestamp() * 1000)
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": self.sign(ts, method, path),
            "KALSHI-ACCESS-TIMESTAMP": str(ts),
        }
