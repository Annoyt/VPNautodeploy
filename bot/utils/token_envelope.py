"""Carry one short secret from the dashboard page to the bot, sealed.

The dashboard reaches the bot through exit's Caddy, and the hop from exit
to entry:8080 is plain HTTP over the public internet (on 2026-11-11:
``reverse_proxy 130.49.146.10:8080``). Whatever the page posts crosses it
in the clear. The page seals the bot token (Settings → «Токен бота») first:

* the bot hands out a one-time Diffie-Hellman share ``B = 2^b mod p`` in
  the RFC 7919 ``ffdhe2048`` group (``ChallengeStore.new``);
* the page picks ``a``, sends ``A = 2^a mod p``, and both sides get
  ``S = B^a = A^b`` (JS BigInt on the page, ``pow`` here);
* HKDF-SHA256(S, salt = challenge id, info = LABEL || A || B) gives an
  encryption key and a MAC key (WebCrypto HKDF on the page);
* ciphertext = plaintext XOR HMAC-SHA256(k_enc, counter) blocks, and
  tag = HMAC-SHA256(k_mac, LABEL || id || A || ciphertext), checked before
  anything is decrypted.

Stdlib only: the image has no crypto library (``cryptography`` is dev-only,
AGENTS.md §40). These are the standard pieces (modular exponentiation in a
standard group, HKDF, HMAC), put together the usual way.

What this covers: someone who RECORDS that hop never sees the secret. What
it does not: someone who can CHANGE traffic on the hop can swap ``B`` for
their own share, or the page's JavaScript itself, which crosses the same
hop. Only TLS on the hop closes that.
"""

import base64
import binascii
import hashlib
import hmac
import re
import secrets
import threading
import time
from typing import Optional

# RFC 7919 ffdhe2048: p = 2^2048 - 2^1984 + {[2^1918 * e] + 560316} * 2^64 - 1,
# a safe prime; g = 2 generates the subgroup of prime order q = (p - 1) / 2.
# tests/unit/test_token_envelope.py derives p from that formula again.
FFDHE2048_P = int(
    'FFFFFFFFFFFFFFFFADF85458A2BB4A9AAFDC5620273D3CF1D8B9C583CE2D3695'
    'A9E13641146433FBCC939DCE249B3EF97D2FE363630C75D8F681B202AEC4617A'
    'D3DF1ED5D5FD65612433F51F5F066ED0856365553DED1AF3B557135E7F57C935'
    '984F0C70E0E68B77E2A689DAF3EFE8721DF158A136ADE73530ACCA4F483A797A'
    'BC0AB182B324FB61D108A94BB2C8E3FBB96ADAB760D7F4681D4F42A3DE394DF4'
    'AE56EDE76372BB190B07A7C8EE0A6D709E02FCE1CDF7E2ECC03404CD28342F61'
    '9172FE9CE98583FF8E4F1232EEF28183C3FE3B1B4C6FAD733BB5FCBC2EC22005'
    'C58EF1837D1683B2C6F34A26C1B2EFFA886B423861285C97FFFFFFFFFFFFFFFF',
    16,
)
FFDHE2048_Q = (FFDHE2048_P - 1) // 2
GENERATOR = 2
GROUP = 'ffdhe2048'
SHARE_BYTES = 256
# RFC 7919 §5.2: an exponent of at least 225 bits for ffdhe2048.
EXPONENT_BITS = 256
LABEL = b'nekovpn bot-token v1'
CHALLENGE_TTL_S = 600
MAX_CHALLENGES = 16
MAX_PLAINTEXT = 256
_HEX_RE = re.compile(r'^[0-9a-fA-F]{1,512}$')
_B64URL_RE = re.compile(r'^[A-Za-z0-9_-]*={0,2}$')


class EnvelopeError(Exception):
    """The envelope cannot be opened. Says why, never what was inside."""


def hkdf_sha256(ikm: bytes, salt: bytes, info: bytes, length: int) -> bytes:
    """RFC 5869 HKDF with SHA-256."""
    prk = hmac.new(salt or bytes(32), ikm, hashlib.sha256).digest()
    okm, block, i = b'', b'', 1
    while len(okm) < length:
        block = hmac.new(prk, block + info + bytes([i]), hashlib.sha256).digest()
        okm += block
        i += 1
    return okm[:length]


def _share_bytes(y: int) -> bytes:
    return y.to_bytes(SHARE_BYTES, 'big')


def check_share(y: int) -> None:
    """``1 < y < p - 1`` and ``y`` in the subgroup ``g`` generates."""
    if not 1 < y < FFDHE2048_P - 1 or pow(y, FFDHE2048_Q, FFDHE2048_P) != 1:
        raise EnvelopeError('share outside the group')


def _keys(shared: int, cid: str, a_share: int, b_share: int):
    okm = hkdf_sha256(
        _share_bytes(shared), cid.encode('ascii'),
        LABEL + _share_bytes(a_share) + _share_bytes(b_share), 64,
    )
    return okm[:32], okm[32:]


def _keystream(k_enc: bytes, n: int) -> bytes:
    out, i = b'', 0
    while len(out) < n:
        out += hmac.new(k_enc, i.to_bytes(4, 'big'), hashlib.sha256).digest()
        i += 1
    return out[:n]


def _tag(k_mac: bytes, cid: str, a_share: int, ct: bytes) -> bytes:
    return hmac.new(k_mac, LABEL + cid.encode('ascii') + _share_bytes(a_share) + ct,
                    hashlib.sha256).digest()


def _xor(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b))


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def _b64url_decode(value, what: str) -> bytes:
    if not isinstance(value, str) or not _B64URL_RE.match(value):
        raise EnvelopeError(f'{what}: not base64url')
    try:
        return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))
    except (binascii.Error, ValueError):
        raise EnvelopeError(f'{what}: not base64url') from None


def _random_exponent() -> int:
    while True:
        x = secrets.randbits(EXPONENT_BITS)
        if x > 1:
            return x


class ChallengeStore:
    """One-time shares the bot handed out; each opens exactly one envelope.

    In memory only: a restart forgets them, and the page asks for a new one.
    An envelope is taken out of the store before it is checked, so a share
    that met a bad envelope cannot be tried again.
    """

    def __init__(self, ttl: float = CHALLENGE_TTL_S, max_entries: int = MAX_CHALLENGES,
                 clock=time.monotonic):
        self.ttl = ttl
        self.max_entries = max_entries
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict = {}  # id -> (b, B, created)

    def new(self) -> dict:
        b = _random_exponent()
        b_share = pow(GENERATOR, b, FFDHE2048_P)
        cid = secrets.token_urlsafe(16)
        now = self._clock()
        with self._lock:
            self._drop_expired(now)
            while len(self._entries) >= self.max_entries:
                oldest = min(self._entries, key=lambda k: self._entries[k][2])
                del self._entries[oldest]
            self._entries[cid] = (b, b_share, now)
        return {'id': cid, 'group': GROUP, 'B': format(b_share, 'x'),
                'ttl': int(self.ttl)}

    def _drop_expired(self, now: float) -> None:
        for k in [k for k, v in self._entries.items() if now - v[2] >= self.ttl]:
            del self._entries[k]

    def open(self, cid, a_hex, ct_b64, tag_b64) -> bytes:
        """The plaintext of an envelope sealed against challenge ``cid``."""
        if not isinstance(cid, str) or not cid:
            raise EnvelopeError('no challenge id')
        now = self._clock()
        with self._lock:
            entry = self._entries.pop(cid, None)
            self._drop_expired(now)
        if entry is None:
            raise EnvelopeError('unknown or used challenge')
        b, b_share, created = entry
        if now - created >= self.ttl:
            raise EnvelopeError('challenge expired')
        if not isinstance(a_hex, str) or not _HEX_RE.match(a_hex):
            raise EnvelopeError('A: not hex')
        a_share = int(a_hex, 16)
        check_share(a_share)
        ct = _b64url_decode(ct_b64, 'ct')
        tag = _b64url_decode(tag_b64, 'tag')
        if not 0 < len(ct) <= MAX_PLAINTEXT:
            raise EnvelopeError('ct: bad length')
        shared = pow(a_share, b, FFDHE2048_P)
        k_enc, k_mac = _keys(shared, cid, a_share, b_share)
        if not hmac.compare_digest(tag, _tag(k_mac, cid, a_share, ct)):
            raise EnvelopeError('tag mismatch')
        return _xor(ct, _keystream(k_enc, len(ct)))


def seal(challenge: dict, plaintext: bytes, *, a: Optional[int] = None) -> dict:
    """The page's half, in Python: the envelope ``ChallengeStore.open`` takes.

    For tests (the page itself does this in bot/webapp/app.js).
    """
    if not 0 < len(plaintext) <= MAX_PLAINTEXT:
        raise ValueError('plaintext: bad length')
    cid = challenge['id']
    b_share = int(challenge['B'], 16)
    check_share(b_share)
    a = a if a is not None else _random_exponent()
    a_share = pow(GENERATOR, a, FFDHE2048_P)
    shared = pow(b_share, a, FFDHE2048_P)
    k_enc, k_mac = _keys(shared, cid, a_share, b_share)
    ct = _xor(plaintext, _keystream(k_enc, len(plaintext)))
    return {'id': cid, 'A': format(a_share, 'x'), 'ct': b64url(ct),
            'tag': b64url(_tag(k_mac, cid, a_share, ct))}
