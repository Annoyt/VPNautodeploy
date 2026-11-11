"""bot/utils/token_envelope.py — the sealed hop for the bot token.

The page in a real browser is the other half (tests/e2e: TestBotTokenSettings).
Here: the group is the RFC 7919 one, HKDF is RFC 5869's, and every way an
envelope can be wrong is refused without opening it.
"""

import random

import pytest

from bot.utils import token_envelope as te

TOKEN = b'7012345678:AAE' + b'n' * 32


def _floor_2k_e(k: int, guard: int = 128) -> int:
    """floor(2^k * e) from e = sum 1/n!, in integers."""
    scale = 1 << (k + guard)
    total, fact, n = 0, 1, 0
    while scale // fact:
        total += scale // fact
        n += 1
        fact *= n
    return total >> guard


def _probable_prime(n: int, rounds: int = 32) -> bool:
    d, s = n - 1, 0
    while d % 2 == 0:
        d //= 2
        s += 1
    rng = random.Random(7)
    for _ in range(rounds):
        x = pow(rng.randrange(2, n - 2), d, n)
        if x in (1, n - 1):
            continue
        for _ in range(s - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


class TestGroup:

    def test_p_is_ffdhe2048_from_its_formula(self):
        """RFC 7919 A.1: p = 2^2048 - 2^1984 + {[2^1918 * e] + 560316} * 2^64 - 1.
        A typo in the hex would weaken every exchange; the formula catches it."""
        p = 2**2048 - 2**1984 + (_floor_2k_e(1918) + 560316) * 2**64 - 1
        assert te.FFDHE2048_P == p

    def test_p_is_a_safe_prime_and_2_generates_the_big_subgroup(self):
        p, q = te.FFDHE2048_P, te.FFDHE2048_Q
        assert _probable_prime(p) and _probable_prime(q)
        assert pow(te.GENERATOR, q, p) == 1          # order q, not 2q


class TestHkdf:

    def test_rfc5869_case_1(self):
        okm = te.hkdf_sha256(bytes([0x0b]) * 22, bytes(range(13)), bytes(range(0xf0, 0xfa)), 42)
        assert okm.hex() == ('3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db0'
                             '2d56ecc4c5bf34007208d5b887185865')

    def test_matches_the_cryptography_package(self):
        hkdf = pytest.importorskip('cryptography.hazmat.primitives.kdf.hkdf')
        hashes = pytest.importorskip('cryptography.hazmat.primitives.hashes')
        rng = random.Random(3)
        for length in (1, 32, 64, 100):
            ikm, salt, info = (bytes(rng.randrange(256) for _ in range(n)) for n in (256, 22, 532))
            ref = hkdf.HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)
            assert te.hkdf_sha256(ikm, salt, info, length) == ref


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(clock):
    return te.ChallengeStore(clock=clock)


def opened(store, env):
    return store.open(env['id'], env['A'], env['ct'], env['tag'])


class TestRoundTrip:

    def test_seal_then_open(self, store):
        assert opened(store, te.seal(store.new(), TOKEN)) == TOKEN

    def test_longer_than_one_keystream_block(self, store):
        text = bytes(range(200))
        assert opened(store, te.seal(store.new(), text)) == text

    def test_each_challenge_is_new(self, store):
        a, b = store.new(), store.new()
        assert a['id'] != b['id'] and a['B'] != b['B']
        assert a['group'] == 'ffdhe2048' and a['ttl'] == te.CHALLENGE_TTL_S

    def test_the_ciphertext_hides_the_token(self, store):
        env = te.seal(store.new(), TOKEN)
        assert TOKEN.decode() not in str(env)
        assert te.b64url(TOKEN) not in str(env)


class TestRefusals:

    def test_used_once_only(self, store):
        env = te.seal(store.new(), TOKEN)
        opened(store, env)
        with pytest.raises(te.EnvelopeError, match='unknown or used'):
            opened(store, env)

    def test_a_bad_envelope_burns_its_challenge_too(self, store):
        ch = store.new()
        env = te.seal(ch, TOKEN)
        with pytest.raises(te.EnvelopeError, match='tag'):
            store.open(env['id'], env['A'], env['ct'], te.b64url(b'\0' * 32))
        with pytest.raises(te.EnvelopeError, match='unknown or used'):
            opened(store, env)                    # the right tag is too late now

    def test_expired(self, store, clock):
        env = te.seal(store.new(), TOKEN)
        clock.now += te.CHALLENGE_TTL_S
        with pytest.raises(te.EnvelopeError):
            opened(store, env)

    def test_just_before_expiry_is_fine(self, store, clock):
        env = te.seal(store.new(), TOKEN)
        clock.now += te.CHALLENGE_TTL_S - 1
        assert opened(store, env) == TOKEN

    @pytest.mark.parametrize('field', ['ct', 'tag'])
    def test_a_flipped_bit_is_refused(self, store, field):
        env = te.seal(store.new(), TOKEN)
        raw = bytearray(te._b64url_decode(env[field], field))
        raw[0] ^= 1
        env[field] = te.b64url(bytes(raw))
        with pytest.raises(te.EnvelopeError, match='tag'):
            opened(store, env)

    def test_another_share_is_refused(self, store):
        env = te.seal(store.new(), TOKEN)
        env['A'] = format(pow(te.GENERATOR, 12345, te.FFDHE2048_P), 'x')
        with pytest.raises(te.EnvelopeError, match='tag'):
            opened(store, env)

    def test_sealed_for_another_challenge(self, store):
        first, second = store.new(), store.new()
        env = te.seal(first, TOKEN)
        env['id'] = second['id']
        with pytest.raises(te.EnvelopeError, match='tag'):
            opened(store, env)

    @pytest.mark.parametrize('share', [0, 1, te.FFDHE2048_P - 1, te.FFDHE2048_P,
                                       te.FFDHE2048_P + 1])
    def test_share_outside_the_range(self, store, share):
        env = te.seal(store.new(), TOKEN)
        env['A'] = format(share, 'x')
        with pytest.raises(te.EnvelopeError):
            opened(store, env)

    def test_share_outside_the_subgroup(self, store):
        p, q = te.FFDHE2048_P, te.FFDHE2048_Q
        outside = next(n for n in range(3, 100) if pow(n, q, p) != 1)
        env = te.seal(store.new(), TOKEN)
        env['A'] = format(outside, 'x')
        with pytest.raises(te.EnvelopeError, match='group'):
            opened(store, env)

    @pytest.mark.parametrize('field, value', [
        ('A', 'xyz'), ('A', ''), ('A', '0x' + 'f' * 10), ('A', 'f' * 513), ('A', None),
        ('ct', '+/=='), ('ct', ''), ('ct', None), ('tag', '!!'),
        ('id', ''), ('id', None), ('id', 42),
    ])
    def test_malformed_fields(self, store, field, value):
        env = te.seal(store.new(), TOKEN)
        env[field] = value
        with pytest.raises(te.EnvelopeError):
            opened(store, env)

    def test_too_long(self, store):
        ch = store.new()
        env = te.seal(ch, b'x' * te.MAX_PLAINTEXT)
        env['ct'] = te.b64url(b'x' * (te.MAX_PLAINTEXT + 1))
        with pytest.raises(te.EnvelopeError, match='length'):
            opened(store, env)

    def test_seal_refuses_a_bad_share_from_the_bot(self, store):
        ch = store.new()
        ch['B'] = '1'
        with pytest.raises(te.EnvelopeError):
            te.seal(ch, TOKEN)


class TestStore:

    def test_full_store_drops_the_oldest(self, clock):
        store = te.ChallengeStore(max_entries=3, clock=clock)
        issued = []
        for _ in range(4):
            clock.now += 1
            issued.append(store.new())
        with pytest.raises(te.EnvelopeError, match='unknown'):
            opened(store, te.seal(issued[0], TOKEN))
        assert opened(store, te.seal(issued[3], TOKEN)) == TOKEN

    def test_expired_entries_are_swept(self, store, clock):
        store.new()
        clock.now += te.CHALLENGE_TTL_S + 1
        store.new()
        assert len(store._entries) == 1

    def test_the_secret_exponent_is_long(self):
        for _ in range(20):
            assert te._random_exponent().bit_length() >= te.EXPONENT_BITS - 12
