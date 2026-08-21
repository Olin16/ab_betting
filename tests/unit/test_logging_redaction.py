"""Secret redaction in logs.

A leaked private key is unrecoverable. Redaction is therefore enforced by a processor
that sees every event, not by discipline at call sites.
"""

from __future__ import annotations

from event_arb.observability.logging import REDACTED, redact_processor, redact_value

# Assembled at runtime rather than written as a literal: a real PEM header in a tracked
# file would (correctly) trip the repository's own secret scan.
_MARKER = "-" * 5 + "{} RSA PRIVATE KEY" + "-" * 5
PEM = "\n".join(
    [
        _MARKER.format("BEGIN"),
        "MIIEowIBAAKCAQEAvSecretKeyMaterialThatMustNeverAppearInLogs",
        _MARKER.format("END"),
    ]
)


def process(event: dict[str, object]) -> dict[str, object]:
    return dict(redact_processor(None, "info", event))  # type: ignore[arg-type]


def test_sensitive_keys_are_redacted_by_name() -> None:
    for key in (
        "api_key",
        "private_key",
        "signature",
        "authorization",
        "password",
        "token",
        "wallet",
        "mnemonic",
        "seed",
        "passphrase",
        "cookie",
    ):
        assert process({key: "super-secret"})[key] == REDACTED


def test_key_matching_is_case_insensitive_and_substring_based() -> None:
    assert process({"KALSHI_ACCESS_SIGNATURE": "abc"})["KALSHI_ACCESS_SIGNATURE"] == REDACTED
    assert process({"userApiKeyId": "abc"})["userApiKeyId"] == REDACTED


def test_pem_blocks_are_stripped_from_free_text() -> None:
    """Even under a harmless key name, key material must never survive."""
    result = process({"detail": f"failed to parse {PEM}"})
    assert "SecretKeyMaterial" not in str(result["detail"])
    assert REDACTED in str(result["detail"])


def test_long_opaque_blobs_are_redacted() -> None:
    signature = "a" * 64
    assert REDACTED in str(process({"detail": f"sig={signature}"})["detail"])


def test_hex_private_keys_are_redacted() -> None:
    assert REDACTED in str(process({"note": "0x" + "ab" * 32})["note"])


def test_nested_structures_are_redacted_recursively() -> None:
    event = {"request": {"headers": {"Authorization": "Bearer abc"}, "path": "/markets"}}
    result = process(event)
    request = result["request"]
    assert isinstance(request, dict)
    headers = request["headers"]
    assert isinstance(headers, dict)
    assert headers["Authorization"] == REDACTED
    assert request["path"] == "/markets"


def test_lists_and_tuples_are_traversed() -> None:
    redacted = redact_value([{"secret": "x"}, {"safe": "keep"}])
    assert isinstance(redacted, list)
    assert redacted[0]["secret"] == REDACTED
    assert redacted[1]["safe"] == "keep"


def test_ordinary_values_pass_through_unchanged() -> None:
    event = {"market_id": "KXFED-26SEP", "quantity": 100, "price": "0.55"}
    assert process(dict(event)) == event


def test_signed_request_payloads_are_not_logged_verbatim() -> None:
    event = {"signed_request": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefg"}
    assert process(event)["signed_request"] == REDACTED
