"""Kalshi request signing.

Signing is tested against a throwaway RSA key generated in-process. No real credential is
involved, and none is required: these tests verify determinism, message construction, and
verifiability, not server acceptance (see docs/api-assumptions.md A5).
"""

from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from event_arb.venues.base import AuthenticationError
from event_arb.venues.kalshi.auth import KalshiSigner, load_private_key, signature_message


@pytest.fixture(scope="module")
def private_key() -> rsa.RSAPrivateKey:
    """A disposable key that exists only for the lifetime of this test module."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def signer(private_key: rsa.RSAPrivateKey) -> KalshiSigner:
    return KalshiSigner(api_key_id="test-key-id", private_key=private_key)


def test_signature_message_format() -> None:
    assert signature_message(1700000000000, "get", "/trade-api/v2/markets") == (
        "1700000000000GET/trade-api/v2/markets"
    )


def test_signature_verifies_against_the_public_key(
    signer: KalshiSigner, private_key: rsa.RSAPrivateKey
) -> None:
    signature = signer.sign(1700000000000, "GET", "/trade-api/v2/markets")
    private_key.public_key().verify(
        base64.b64decode(signature),
        b"1700000000000GET/trade-api/v2/markets",
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


def test_pss_signatures_are_salted_and_therefore_differ(signer: KalshiSigner) -> None:
    """RSA-PSS is randomized; identical inputs must not produce identical signatures."""
    first = signer.sign(1700000000000, "GET", "/x")
    second = signer.sign(1700000000000, "GET", "/x")
    assert first != second


def test_headers_contain_all_three_required_fields(signer: KalshiSigner) -> None:
    headers = signer.headers("GET", "/trade-api/v2/markets", timestamp_ms=1700000000000)
    assert headers["KALSHI-ACCESS-KEY"] == "test-key-id"
    assert headers["KALSHI-ACCESS-TIMESTAMP"] == "1700000000000"
    assert headers["KALSHI-ACCESS-SIGNATURE"]


def test_different_paths_produce_different_signatures(signer: KalshiSigner) -> None:
    """A signature must be bound to its request, not reusable across endpoints."""
    message_a = signature_message(1, "GET", "/a")
    message_b = signature_message(1, "GET", "/b")
    assert message_a != message_b


def test_missing_key_file_raises_authentication_error(tmp_path: object) -> None:
    from pathlib import Path

    assert isinstance(tmp_path, Path)
    with pytest.raises(AuthenticationError):
        load_private_key(tmp_path / "nonexistent.pem")


def test_non_rsa_key_rejected(tmp_path: object) -> None:
    from pathlib import Path

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    assert isinstance(tmp_path, Path)
    key_path = tmp_path / "ed25519.pem"
    key_path.write_bytes(
        ed25519.Ed25519PrivateKey.generate().private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    with pytest.raises(AuthenticationError):
        load_private_key(key_path)
