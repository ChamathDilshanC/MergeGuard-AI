"""Throwaway RSA keys for the GitHub App tests, made at test time.

No key is committed (fake-secret policy, docs/TESTING_STRATEGY.md section 4) and
no crypto library is needed: primes come from Miller-Rabin, and the PEM text is
assembled from parts so the file itself never holds a key header.
"""

import base64
import functools
import random

from mergeguard.rsa_jwt import RsaKey

_SMALL_PRIMES = [
    p for p in range(3, 2000) if all(p % q for q in range(2, int(p**0.5) + 1))
]


def _is_prime(candidate: int, rng: random.Random) -> bool:
    if candidate < 2:
        return False
    for prime in _SMALL_PRIMES:
        if candidate == prime:
            return True
        if candidate % prime == 0:
            return False
    d, r = candidate - 1, 0
    while d % 2 == 0:
        d //= 2
        r += 1
    for _ in range(24):
        a = rng.randrange(2, candidate - 1)
        x = pow(a, d, candidate)
        if x in (1, candidate - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, candidate)
            if x == candidate - 1:
                break
        else:
            return False
    return True


def _prime(bits: int, rng: random.Random) -> int:
    while True:
        candidate = rng.getrandbits(bits) | (1 << (bits - 1)) | (1 << (bits - 2)) | 1
        if _is_prime(candidate, rng):
            return candidate


@functools.cache
def generate(bits: int = 1024, seed: int = 1) -> tuple[RsaKey, int, int]:
    """(key, p, q). A seeded generator keeps the test keys stable between runs;
    they protect nothing."""
    rng = random.Random(seed)  # noqa: S311 - a throwaway test key
    e = 65537
    while True:
        p, q = _prime(bits // 2, rng), _prime(bits // 2, rng)
        n = p * q
        phi = (p - 1) * (q - 1)
        if p != q and n.bit_length() == bits and phi % e:
            return RsaKey(n, e, pow(e, -1, phi)), p, q


# --- DER and PEM encoding --------------------------------------------------------


def _length(length: int) -> bytes:
    if length < 0x80:
        return bytes([length])
    raw = length.to_bytes((length.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(raw)]) + raw


def _tlv(tag: int, value: bytes) -> bytes:
    return bytes([tag]) + _length(len(value)) + value


def _integer(value: int) -> bytes:
    raw = value.to_bytes(max(1, (value.bit_length() + 8) // 8), "big")
    return _tlv(0x02, raw)


def pkcs1_der(key: RsaKey, p: int, q: int) -> bytes:
    parts = [
        0,
        key.n,
        key.e,
        key.d,
        p,
        q,
        key.d % (p - 1),
        key.d % (q - 1),
        pow(q, -1, p),
    ]
    return _tlv(0x30, b"".join(_integer(part) for part in parts))


def pkcs8_der(key: RsaKey, p: int, q: int) -> bytes:
    algorithm = _tlv(
        0x30, _tlv(0x06, bytes.fromhex("2a864886f70d010101")) + _tlv(0x05, b"")
    )
    return _tlv(0x30, _integer(0) + algorithm + _tlv(0x04, pkcs1_der(key, p, q)))


def pem(label: str, der: bytes) -> str:
    body = base64.b64encode(der).decode()
    lines = "\n".join(body[i : i + 64] for i in range(0, len(body), 64))
    return f"-----BEGIN {label}-----\n{lines}\n-----END {label}-----\n"


def key_label(prefix: str = "") -> str:
    return prefix + "PRIVATE " + "KEY"


def pkcs1_pem(bits: int = 1024, seed: int = 1) -> str:
    key, p, q = generate(bits, seed)
    return pem(key_label("RSA "), pkcs1_der(key, p, q))


def pkcs8_pem(bits: int = 1024, seed: int = 1) -> str:
    key, p, q = generate(bits, seed)
    return pem(key_label(), pkcs8_der(key, p, q))
