import threading
from datetime import datetime, timedelta, timezone

import pytest

from otpguard import (
    AttemptRecord,
    LockedOut,
    LockoutPolicy,
    MemoryStorage,
    Message,
    ResendCooldown,
    ResendTooSoon,
    StubSender,
    generate_code,
    hash_code,
    verify_code,
)

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
COOLDOWN = timedelta(seconds=60)
CODE_TTL = timedelta(minutes=5)
LOCKOUT = timedelta(minutes=15)
MAX_ATTEMPTS = 3
IDENTIFIER = "alice"
DESTINATION = "+15551234567"
CODE = "482913"
OTHER = "314159"
WRONG = "000000"
THREADS = 16


class Clock:
    """The only source of time in these tests: nothing here sleeps."""

    def __init__(self, start: datetime = NOW) -> None:
        self._now = start

    def utcnow(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._now.timestamp()

    def advance(self, delta: timedelta) -> None:
        self._now += delta


class Flow:
    """A verification flow assembled from the pieces the library ships.

    The pending digest, the moment of the last delivery and the attempt tally
    all live in storage, so the whole flow runs on the injected clock.
    """

    def __init__(self, clock: Clock, *, max_attempts: int = MAX_ATTEMPTS) -> None:
        self.clock = clock
        self.store = MemoryStorage(clock=clock.monotonic)
        self.cooldown = ResendCooldown(COOLDOWN)
        self.lockout = LockoutPolicy(max_attempts=max_attempts, duration=LOCKOUT)
        self.sender = StubSender()

    def key(self, kind: str, identifier: str = IDENTIFIER) -> str:
        return f"{kind}:{identifier}"

    def request(self, identifier: str = IDENTIFIER, code: str | None = None) -> str:
        now = self.clock.utcnow()
        self.cooldown.check(self.last_sent(identifier), now)
        code = generate_code() if code is None else code
        self.store.set(self.key("code", identifier), hash_code(code).encode(), CODE_TTL)
        self.store.set(self.key("sent", identifier), now.isoformat(), CODE_TTL)
        self.store.delete(self.key("used", identifier))
        self.sender.send(Message(DESTINATION, code))
        return code

    def submit(self, candidate: str, identifier: str = IDENTIFIER) -> bool:
        now = self.clock.utcnow()
        self.lockout.check(self.record(identifier), now)
        code_key = self.key("code", identifier)
        used_key = self.key("used", identifier)
        stored = self.store.get(code_key)
        if stored is not None and verify_code(candidate, stored):
            # One code, one success: the increment is the part that must be atomic.
            if self.store.incr(used_key, CODE_TTL) != 1:
                return False
            self.store.delete(code_key)
            self.store.delete(self.key("fails", identifier))
            return True
        if stored is None and self.store.get(used_key) is not None:
            return False
        failed = self.lockout.register_failure(self.record(identifier), now)
        self.store.set(
            self.key("fails", identifier), failed.encode(), LOCKOUT + CODE_TTL
        )
        return False

    def record(self, identifier: str = IDENTIFIER) -> str | None:
        return self.store.get(self.key("fails", identifier))

    def last_sent(self, identifier: str = IDENTIFIER) -> datetime | None:
        raw = self.store.get(self.key("sent", identifier))
        return None if raw is None else datetime.fromisoformat(raw)

    def remaining(self, identifier: str = IDENTIFIER) -> int:
        return self.lockout.remaining_attempts(
            self.record(identifier), self.clock.utcnow()
        )


def run_concurrently(action, count: int = THREADS) -> list:
    """Run the action in `count` threads released at the same moment."""
    barrier = threading.Barrier(count)
    outcomes: list = [None] * count

    def worker(index: int) -> None:
        barrier.wait()
        try:
            outcomes[index] = action()
        except Exception as exc:
            outcomes[index] = exc

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return outcomes


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def flow(clock):
    return Flow(clock)


def test_a_generated_code_verifies_once(flow):
    code = flow.request()
    assert flow.sender.last.body.endswith("verification code.")
    assert flow.submit(code)
    assert flow.store.get(flow.key("code")) is None
    assert not flow.submit(code)
    assert flow.remaining() == MAX_ATTEMPTS


def test_a_resend_waits_for_the_cooldown(flow):
    flow.request(code=CODE)

    with pytest.raises(ResendTooSoon) as excinfo:
        flow.request(code=OTHER)
    assert excinfo.value.retry_after == COOLDOWN

    flow.clock.advance(timedelta(seconds=59))
    with pytest.raises(ResendTooSoon) as excinfo:
        flow.request(code=OTHER)
    assert excinfo.value.retry_after == timedelta(seconds=1)

    flow.clock.advance(timedelta(seconds=1))
    assert flow.request(code=OTHER) == OTHER
    assert len(flow.sender.sent) == 2


def test_a_resend_replaces_the_pending_code(flow):
    flow.request(code=CODE)
    flow.clock.advance(COOLDOWN)
    flow.request(code=OTHER)
    assert not flow.submit(CODE)
    assert flow.submit(OTHER)


def test_wrong_codes_lock_the_identifier(flow):
    flow.request(code=CODE)
    assert flow.remaining() == MAX_ATTEMPTS

    for left in (2, 1, 0):
        assert not flow.submit(WRONG)
        assert flow.remaining() == left

    with pytest.raises(LockedOut) as excinfo:
        flow.submit(CODE)
    assert excinfo.value.retry_after == LOCKOUT
    assert excinfo.value.retry_after_seconds == pytest.approx(900.0)


def test_the_lock_expires_and_the_tally_starts_over(flow):
    flow.request(code=CODE)
    for _ in range(MAX_ATTEMPTS):
        assert not flow.submit(WRONG)

    flow.clock.advance(LOCKOUT - timedelta(seconds=1))
    with pytest.raises(LockedOut) as excinfo:
        flow.submit(CODE)
    assert excinfo.value.retry_after == timedelta(seconds=1)

    flow.clock.advance(timedelta(seconds=1))
    assert flow.remaining() == MAX_ATTEMPTS
    flow.request(code=OTHER)
    assert flow.submit(OTHER)


def test_hammering_a_locked_identifier_does_not_stretch_the_lock(flow):
    flow.request(code=CODE)
    for _ in range(MAX_ATTEMPTS):
        flow.submit(WRONG)
    locked = flow.record()

    for _ in range(5):
        flow.clock.advance(timedelta(minutes=1))
        with pytest.raises(LockedOut):
            flow.submit(WRONG)

    assert flow.record() == locked
    assert flow.lockout.retry_after(locked, flow.clock.utcnow()) == timedelta(minutes=10)


def test_a_pending_code_verifies_until_its_lifetime_runs_out(flow):
    flow.request(code=CODE)
    flow.clock.advance(CODE_TTL - timedelta(seconds=1))
    assert flow.submit(CODE)


def test_an_expired_code_is_refused_and_a_new_one_may_be_sent(flow):
    flow.request(code=CODE)
    flow.clock.advance(CODE_TTL)
    assert flow.store.get(flow.key("code")) is None
    assert not flow.submit(CODE)
    assert flow.remaining() == MAX_ATTEMPTS - 1

    flow.request(code=OTHER)
    assert len(flow.sender.sent) == 2
    assert flow.submit(OTHER)


def test_identifiers_do_not_share_a_cooldown_or_a_tally(flow):
    flow.request("alice", code=CODE)
    flow.request("bob", code=OTHER)

    for _ in range(MAX_ATTEMPTS):
        assert not flow.submit(WRONG, "alice")
    with pytest.raises(LockedOut):
        flow.submit(CODE, "alice")

    assert flow.remaining("bob") == MAX_ATTEMPTS
    assert flow.submit(OTHER, "bob")


def test_only_one_concurrent_submission_can_spend_a_code(flow):
    flow.request(code=CODE)
    outcomes = run_concurrently(lambda: flow.submit(CODE))
    assert outcomes.count(True) == 1
    assert outcomes.count(False) == THREADS - 1
    assert flow.store.get(flow.key("code")) is None
    assert flow.remaining() == MAX_ATTEMPTS


def test_no_concurrent_submission_slips_past_a_lock(flow):
    flow.request(code=CODE)
    locked = AttemptRecord(
        failures=MAX_ATTEMPTS, locked_until=flow.clock.utcnow() + LOCKOUT
    )
    flow.store.set(flow.key("fails"), locked.encode(), LOCKOUT)

    outcomes = run_concurrently(lambda: flow.submit(CODE))
    assert all(isinstance(outcome, LockedOut) for outcome in outcomes)
    assert flow.store.get(flow.key("used")) is None


def test_a_concurrent_tally_cannot_outrun_the_limit(flow):
    attempts = run_concurrently(lambda: flow.store.incr(flow.key("fails"), LOCKOUT))
    assert sorted(attempts) == list(range(1, THREADS + 1))
    assert len([n for n in attempts if n <= MAX_ATTEMPTS]) == MAX_ATTEMPTS


def test_a_concurrent_resend_claim_is_won_once_and_expires_with_the_window(flow):
    claims = run_concurrently(lambda: flow.store.incr(flow.key("sent"), COOLDOWN))
    assert claims.count(1) == 1

    flow.clock.advance(COOLDOWN - timedelta(seconds=1))
    assert flow.store.incr(flow.key("sent"), COOLDOWN) == THREADS + 1
    flow.clock.advance(timedelta(seconds=1))
    assert flow.store.incr(flow.key("sent"), COOLDOWN) == 1
