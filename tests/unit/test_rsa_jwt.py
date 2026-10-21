"""RS256 signing for GitHub App JWTs, standard library only (FR-020, ADR-0010).

The signer was also checked against OpenSSL 3.5 for PKCS#1 and PKCS#8 keys of
2048 and 3072 bits; that one-off check is recorded in the PR, not repeated here,
because the tests must not depend on a system tool.
"""

import base64
import json

import pytest

import rsa_support
from mergeguard import rsa_jwt
from mergeguard.rsa_jwt import (
    MIN_KEY_BITS,
    PrivateKeyError,
    make_jwt,
    parse_private_key,
    sign_rs256,
    verify_rs256,
)

SMALL = 1024  # the tests relax the 2048-bit floor to keep key generation fast


def small_key(pem_text: str | None = None):
    return parse_private_key(pem_text or rsa_support.pkcs1_pem(SMALL), min_bits=SMALL)


def b64d(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


# --- key parsing ----------------------------------------------------------------


@pytest.mark.parametrize("make_pem", [rsa_support.pkcs1_pem, rsa_support.pkcs8_pem])
def test_both_private_key_formats_parse_to_the_same_key(make_pem):
    key, _, _ = rsa_support.generate(SMALL)
    parsed = parse_private_key(make_pem(SMALL), min_bits=SMALL)
    assert (parsed.n, parsed.e, parsed.d) == (key.n, key.e, key.d)
    assert parsed.bits == SMALL and parsed.size == SMALL // 8


def test_the_private_exponent_is_not_in_repr():
    key = small_key()
    assert str(key.d) not in repr(key)


def test_a_2048_bit_key_meets_the_default_floor():
    pem_text = rsa_support.pkcs8_pem(MIN_KEY_BITS)
    assert parse_private_key(pem_text).bits == MIN_KEY_BITS


def test_keys_below_the_floor_are_refused():
    with pytest.raises(PrivateKeyError, match="size"):
        parse_private_key(rsa_support.pkcs1_pem(SMALL))


def test_oversized_keys_are_refused(monkeypatch):
    monkeypatch.setattr(rsa_jwt, "MAX_KEY_BITS", SMALL - 1)
    with pytest.raises(PrivateKeyError, match="size"):
        parse_private_key(rsa_support.pkcs1_pem(SMALL), min_bits=SMALL)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not a key",
        "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----",
        "-----BEGIN "
        + "ENCRYPTED PRIVATE "
        + "KEY-----\nAAAA\n-----END "
        + "ENCRYPTED PRIVATE "
        + "KEY-----",
        "-----BEGIN "
        + "RSA PRIVATE "
        + "KEY-----\n!!!!\n-----END "
        + "RSA PRIVATE "
        + "KEY-----",
        "-----BEGIN "
        + "EC PRIVATE "
        + "KEY-----\nAAAA\n-----END "
        + "EC PRIVATE "
        + "KEY-----",
    ],
)
def test_unusable_keys_are_refused(text):
    with pytest.raises(PrivateKeyError):
        parse_private_key(text, min_bits=SMALL)


def test_truncated_and_trailing_der_is_refused():
    key, p, q = rsa_support.generate(SMALL)
    der = rsa_support.pkcs1_der(key, p, q)
    label = rsa_support.key_label("RSA ")
    for broken in (der[:-5], der + b"\x00", b"\x30\x80" + der[2:]):
        with pytest.raises(PrivateKeyError):
            parse_private_key(rsa_support.pem(label, broken), min_bits=SMALL)


def test_a_pkcs8_key_for_another_algorithm_is_refused():
    key, p, q = rsa_support.generate(SMALL)
    ec_oid = bytes.fromhex("2a8648ce3d0201")  # id-ecPublicKey
    algorithm = rsa_support._tlv(0x30, rsa_support._tlv(0x06, ec_oid))
    der = rsa_support._tlv(
        0x30,
        rsa_support._integer(0)
        + algorithm
        + rsa_support._tlv(0x04, rsa_support.pkcs1_der(key, p, q)),
    )
    with pytest.raises(PrivateKeyError, match="not an RSA key"):
        parse_private_key(rsa_support.pem(rsa_support.key_label(), der), min_bits=SMALL)


def test_inconsistent_key_parameters_are_refused():
    key, p, q = rsa_support.generate(SMALL)
    bad = rsa_support._tlv(
        0x30,
        b"".join(rsa_support._integer(v) for v in (0, key.n, 4, key.d, p, q, 1, 1, 1)),
    )
    with pytest.raises(PrivateKeyError, match="invalid"):
        parse_private_key(
            rsa_support.pem(rsa_support.key_label("RSA "), bad), min_bits=SMALL
        )


# --- signing --------------------------------------------------------------------


def test_signatures_verify_and_have_the_key_length():
    key = small_key()
    signature = sign_rs256(key, b"message")
    assert len(signature) == key.size
    assert verify_rs256(key, b"message", signature)


def test_a_signature_does_not_verify_for_another_message_or_key():
    key = small_key()
    signature = sign_rs256(key, b"message")
    assert not verify_rs256(key, b"other message", signature)
    other = parse_private_key(rsa_support.pkcs1_pem(SMALL, seed=2), min_bits=SMALL)
    assert not verify_rs256(other, b"message", signature)
    assert not verify_rs256(key, b"message", signature[:-1])
    assert not verify_rs256(key, b"message", b"\xff" * key.size)


def test_pkcs1_v1_5_signatures_are_deterministic_despite_blinding():
    """Blinding randomises the computation, never the result."""
    key = small_key()
    assert sign_rs256(key, b"same") == sign_rs256(key, b"same")


def test_the_signature_is_the_standard_pkcs1_v1_5_block():
    key = small_key()
    signature = sign_rs256(key, b"abc")
    block = pow(int.from_bytes(signature, "big"), key.e, key.n).to_bytes(
        key.size, "big"
    )
    assert block[:2] == b"\x00\x01" and block[2:10] == b"\xff" * 8
    assert block.endswith(
        bytes.fromhex("3031300d060960864801650304020105000420")
        + bytes.fromhex(
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        )  # SHA-256("abc")
    )


def test_a_key_with_a_wrong_private_exponent_never_releases_a_signature():
    key, p, q = rsa_support.generate(SMALL)
    broken = type(key)(key.n, key.e, key.d + 2)
    with pytest.raises(PrivateKeyError, match="does not match"):
        sign_rs256(broken, b"message")


def test_a_key_too_small_for_sha256_is_refused():
    from mergeguard.rsa_jwt import RsaKey

    tiny = RsaKey((1 << 255) | 1, 3, 3)
    with pytest.raises(PrivateKeyError, match="too small"):
        sign_rs256(tiny, b"message")


# --- JWT ------------------------------------------------------------------------


def test_the_jwt_has_the_claims_github_expects():
    key = small_key()
    token = make_jwt(key, "123456", now=1_700_000_000)
    header, claims, signature = token.split(".")
    assert json.loads(b64d(header)) == {"alg": "RS256", "typ": "JWT"}
    assert json.loads(b64d(claims)) == {
        "iat": 1_700_000_000 - 60,  # backdated for clock drift
        "exp": 1_700_000_000 + 540,  # under GitHub's ten-minute limit
        "iss": "123456",
    }
    assert verify_rs256(key, f"{header}.{claims}".encode(), b64d(signature))


def test_the_jwt_is_url_safe_and_unpadded():
    token = make_jwt(small_key(), "1", now=1_700_000_000)
    assert "=" not in token and "+" not in token and "/" not in token
    assert token.count(".") == 2


# --- remaining malformed inputs and the blinding retry -------------------------


def _pem_of(der: bytes, label: str | None = None) -> str:
    return rsa_support.pem(label or rsa_support.key_label("RSA "), der)


@pytest.mark.parametrize(
    "der",
    [
        b"\x30",  # truncated header
        rsa_support._tlv(0x30, rsa_support._tlv(0x04, b"x") * 4),  # not integers
        rsa_support._tlv(0x30, rsa_support._integer(1) * 4),  # version 1: multi-prime
        rsa_support._tlv(0x30, rsa_support._integer(0)),  # too few items
        rsa_support._tlv(0x04, b"x"),  # not a sequence
    ],
    ids=["truncated", "not-integers", "version-1", "too-few", "not-a-sequence"],
)
def test_malformed_pkcs1_structures_are_refused(der):
    with pytest.raises(PrivateKeyError):
        parse_private_key(_pem_of(der), min_bits=SMALL)


def test_a_malformed_pkcs8_wrapper_is_refused():
    der = rsa_support._tlv(0x30, rsa_support._integer(0) + rsa_support._tlv(0x04, b"x"))
    with pytest.raises(PrivateKeyError, match="PKCS#8"):
        parse_private_key(_pem_of(der, rsa_support.key_label()), min_bits=SMALL)


def test_a_pem_body_that_is_not_base64_is_refused():
    label = rsa_support.key_label("RSA ")
    text = f"-----BEGIN {label}-----\nAAA\n-----END {label}-----"
    with pytest.raises(PrivateKeyError, match="base64"):
        parse_private_key(text, min_bits=SMALL)


def test_a_non_invertible_blinding_factor_is_redrawn(monkeypatch):
    """A factor that shares a prime with n cannot be inverted, so it is skipped
    and the signature still comes out right."""
    key, p, _ = rsa_support.generate(SMALL)
    draws = iter([p - 2, 12345])  # randbelow() + 2: first is p, a factor of n
    monkeypatch.setattr(rsa_jwt.secrets, "randbelow", lambda _: next(draws))
    signature = sign_rs256(key, b"message")
    assert verify_rs256(key, b"message", signature)
