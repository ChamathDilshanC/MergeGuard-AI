"""RS256 JSON Web Tokens for GitHub App authentication, standard library only
(FR-020, ADR-0010).

GitHub Apps authenticate with a JWT signed by the App's RSA private key, and the
standard library has no RSA. This module does the small part that is needed:
read an unencrypted PKCS#1 or PKCS#8 private key, and sign with RSASSA-PKCS1-v1_5
over SHA-256 using Python's big-integer ``pow``.

Hardening, because this is hand-written cryptography:

- the private-key operation is blinded with a fresh random value, so its timing
  does not depend on the message;
- every signature is verified with the public exponent before it is returned,
  which stops a faulty computation from leaking a usable signature;
- the DER reader is strict (definite lengths, no trailing bytes) and the key size
  is bounded;
- nothing here is ever logged, and ``RsaKey`` keeps its secret parts out of
  ``repr``.

It is not a general-purpose RSA library: no encrypted keys, no other algorithms.
"""

import base64
import hashlib
import json
import re
import secrets
from dataclasses import dataclass, field

MIN_KEY_BITS = 2048  # what GitHub issues
MAX_KEY_BITS = 8192
JWT_BACKDATE_SECONDS = 60  # GitHub advises backdating iat for clock drift
JWT_LIFETIME_SECONDS = 540  # GitHub allows at most ten minutes

# DER prefix of DigestInfo for SHA-256 (RFC 8017 section 9.2, note 1).
_SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")
_RSA_ENCRYPTION_OID = bytes.fromhex("2a864886f70d010101")
_PEM_RE = re.compile(
    r"-----BEGIN (?P<label>[A-Z ]+)-----"
    r"(?P<body>[A-Za-z0-9+/=\s]+)"
    r"-----END (?P=label)-----"
)


class PrivateKeyError(ValueError):
    """The private key is missing, malformed, encrypted or the wrong size."""


@dataclass(frozen=True, slots=True)
class RsaKey:
    n: int
    e: int
    d: int = field(repr=False)

    @property
    def bits(self) -> int:
        return self.n.bit_length()

    @property
    def size(self) -> int:
        return (self.bits + 7) // 8


# --- DER -------------------------------------------------------------------------


def _read(data: bytes, offset: int) -> tuple[int, bytes, int]:
    """One TLV: (tag, value, offset after it). Definite lengths only."""
    if offset + 2 > len(data):
        raise PrivateKeyError("truncated DER")
    tag = data[offset]
    first = data[offset + 1]
    offset += 2
    if first < 0x80:
        length = first
    else:
        count = first & 0x7F
        if count == 0 or count > 4 or offset + count > len(data):
            raise PrivateKeyError("unsupported DER length")
        length = int.from_bytes(data[offset : offset + count], "big")
        offset += count
    end = offset + length
    if end > len(data):
        raise PrivateKeyError("truncated DER")
    return tag, data[offset:end], end


def _children(value: bytes) -> list[tuple[int, bytes]]:
    items: list[tuple[int, bytes]] = []
    offset = 0
    while offset < len(value):
        tag, item, offset = _read(value, offset)
        items.append((tag, item))
    return items


def _sequence(data: bytes) -> list[tuple[int, bytes]]:
    tag, value, end = _read(data, 0)
    if tag != 0x30 or end != len(data):
        raise PrivateKeyError("expected one DER sequence")
    return _children(value)


def _integer(item: tuple[int, bytes]) -> int:
    tag, value = item
    if tag != 0x02 or not value:
        raise PrivateKeyError("expected a DER integer")
    return int.from_bytes(value, "big", signed=False)


def _pkcs1(der: bytes) -> RsaKey:
    items = _sequence(der)
    if len(items) < 4 or _integer(items[0]) != 0:  # version 0: two primes
        raise PrivateKeyError("not an RSA private key")
    n, e, d = _integer(items[1]), _integer(items[2]), _integer(items[3])
    return RsaKey(n, e, d)


def _pkcs8(der: bytes) -> RsaKey:
    items = _sequence(der)
    if (
        len(items) < 3
        or items[0][0] != 0x02
        or items[1][0] != 0x30
        or items[2][0] != 0x04
    ):
        raise PrivateKeyError("not a PKCS#8 private key")
    algorithm = _children(items[1][1])
    if not algorithm or algorithm[0] != (0x06, _RSA_ENCRYPTION_OID):
        raise PrivateKeyError("the key is not an RSA key")
    return _pkcs1(items[2][1])


def parse_private_key(pem: str, *, min_bits: int = MIN_KEY_BITS) -> RsaKey:
    """Read an unencrypted RSA private key in PEM form (PKCS#1 or PKCS#8)."""
    match = _PEM_RE.search(pem or "")
    if not match:
        raise PrivateKeyError("no PEM private key found")
    label = match["label"]
    if "ENCRYPTED" in label or "Proc-Type" in pem:
        raise PrivateKeyError("encrypted private keys are not supported")
    try:
        der = base64.b64decode("".join(match["body"].split()), validate=True)
    except ValueError as exc:
        raise PrivateKeyError("the PEM body is not valid base64") from exc
    if label == "RSA PRIVATE KEY":
        key = _pkcs1(der)
    elif label == "PRIVATE KEY":
        key = _pkcs8(der)
    else:
        raise PrivateKeyError("unsupported key type")
    if not min_bits <= key.bits <= MAX_KEY_BITS:
        raise PrivateKeyError("the key size is outside the accepted range")
    if key.e < 3 or key.e % 2 == 0 or key.d <= 1 or key.d >= key.n:
        raise PrivateKeyError("the RSA key parameters are invalid")
    return key


# --- Signing ---------------------------------------------------------------------


def _encode(key: RsaKey, message: bytes) -> int:
    digest_info = _SHA256_DIGEST_INFO + hashlib.sha256(message).digest()
    padding = key.size - len(digest_info) - 3
    if padding < 8:
        raise PrivateKeyError("the key is too small for SHA-256")
    block = b"\x00\x01" + b"\xff" * padding + b"\x00" + digest_info
    return int.from_bytes(block, "big")


def sign_rs256(key: RsaKey, message: bytes) -> bytes:
    """RSASSA-PKCS1-v1_5 signature of ``message`` with SHA-256."""
    encoded = _encode(key, message)
    while True:  # a blinding factor must be invertible modulo n
        blind = secrets.randbelow(key.n - 2) + 2
        try:
            inverse = pow(blind, -1, key.n)
            break
        except ValueError:
            continue
    blinded = (encoded * pow(blind, key.e, key.n)) % key.n
    signature = (pow(blinded, key.d, key.n) * inverse) % key.n
    if pow(signature, key.e, key.n) != encoded:  # never release a faulty signature
        raise PrivateKeyError("the private key does not match its public exponent")
    return signature.to_bytes(key.size, "big")


def verify_rs256(key: RsaKey, message: bytes, signature: bytes) -> bool:
    """Check a signature with the public part of ``key`` (used by the tests)."""
    if len(signature) != key.size:
        return False
    value = int.from_bytes(signature, "big")
    return value < key.n and pow(value, key.e, key.n) == _encode(key, message)


# --- JWT -------------------------------------------------------------------------


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def make_jwt(key: RsaKey, issuer: str, now: int) -> str:
    """A GitHub App JWT: issued a minute in the past, valid for nine minutes."""
    header = {"alg": "RS256", "typ": "JWT"}
    claims = {
        "iat": now - JWT_BACKDATE_SECONDS,
        "exp": now + JWT_LIFETIME_SECONDS,
        "iss": issuer,
    }
    signing_input = ".".join(
        _b64url(json.dumps(part, separators=(",", ":")).encode("utf-8"))
        for part in (header, claims)
    )
    signature = sign_rs256(key, signing_input.encode("ascii"))
    return f"{signing_input}.{_b64url(signature)}"
