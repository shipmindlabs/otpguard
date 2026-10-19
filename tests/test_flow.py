import threading
from datetime import datetime, timedelta, timezone

import pytest

from otpguard import (
    UPPERCASE_UNAMBIGUOUS,
    CodePolicy,
    DeliveryError,
    LockedOut,
    LockoutPolicy,
    MemoryStorage,
    Message,
    OtpFlow,
    RequestedCode,
    ResendCooldown,
    ResendTooSoon,
    StubSender,
    verify_code,
)

# Aliased: pytest would try to collect anything named TestMode as a test class.
from otpguard import TestMode as Mode

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
COOLDOWN = timedelta(seconds=60)
CODE_TTL = timedelta(minutes=5)
LOCKOUT = timedelta(minutes=15)
MAX_ATTEMPTS = 3
IDENTIFIER = "alice"
DESTINATION = "+15551234567"
UNIVERSAL = "000111"
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


class RefusingSender:
    """A channel that is reachable and still will not take the message."""

    def send(self, message: Message) -> None:
        raise DeliveryError("the gateway refused the message")


def wrong_code(code: str) -> str:
    """A candidate that is certainly not the issued code."""
    return WRONG if code != WRONG else "111111"


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
def store(clock):
    return MemoryStorage(clock=clock.monotonic)


@pytest.fixture
def sender():
    return StubSender()


@pytest.fixture
def flow(store, sender):
    return OtpFlow(
        store,
        sender,
        code_ttl=CODE_TTL,
        cooldown=ResendCooldown(COOLDOWN),
        lockout=LockoutPolicy(max_attempts=MAX_ATTEMPTS, duration=LOCKOUT),
    )


def test_a_requested_code_is_delivered_and_verifies_once(flow, clock, sender):
    issued = flow.request(IDENTIFIER, DESTINATION, now=clock.utcnow())
    assert sender.last.destination == DESTINATION
    assert sender.last.code == issued.code

    assert flow.verify(IDENTIFIER, issued.code, now=clock.utcnow())
    assert not flow.verify(IDENTIFIER, issued.code, now=clock.utcnow())
    # A repeat of a success is not a guess, so it costs no attempt.
    assert flow.remaining_attempts(IDENTIFIER, now=clock.utcnow()) == MAX_ATTEMPTS


def test_the_receipt_describes_the_pending_code(flow, clock):
    issued = flow.request(IDENTIFIER, DESTINATION, now=clock.utcnow())
    assert issued.identifier == IDENTIFIER
    assert issued.expires_at == NOW + CODE_TTL
    assert issued.resend_allowed_at == NOW + COOLDOWN
    assert issued.code not in repr(issued)
    assert DESTINATION in repr(issued)


def test_only_a_digest_reaches_the_store(flow, clock, store):
    issued = flow.request(IDENTIFIER, DESTINATION, now=clock.utcnow())
    stored = store.get(f"code:{IDENTIFIER}")
    assert issued.code not in stored
    assert verify_code(issued.code, stored)


def test_a_resend_waits_for_the_cooldown(flow, clock, sender):
    flow.request(IDENTIFIER, DESTINATION, now=clock.utcnow())

    with pytest.raises(ResendTooSoon) as excinfo:
        flow.request(IDENTIFIER, DESTINATION, now=clock.utcnow())
    assert excinfo.value.retry_after == COOLDOWN

    clock.advance(timedelta(seconds=59))
    with pytest.raises(ResendTooSoon) as excinfo:
        flow.request(IDENTIFIER, DESTINATION, now=clock.utcnow())
    assert excinfo.value.retry_after == timedelta(seconds=1)
    assert excinfo.value.retry_after_seconds == pytest.approx(1.0)

    clock.advance(timedelta(seconds=1))
    flow.request(IDENTIFIER, DESTINATION, now=clock.utcnow())
    assert len(sender.sent) == 2


def test_a_resend_replaces_the_digest_but_not_the_tally(flow, clock):
    first = flow.request(IDENTIFIER, DESTINATION, now=clock.utcnow())
    assert not flow.verify(IDENTIFIER, wrong_code(first.code), now=clock.utcnow())
    assert flow.remaining_attempts(IDENTIFIER, now=clock.utcnow()) == MAX_ATTEMPTS - 1

    clock.advance(COOLDOWN)
    second = flow.request(IDENTIFIER, DESTINATION, now=clock.utcnow())
    assert flow.remaining_attempts(IDENTIFIER, now=clock.utcnow()) == MAX_ATTEMPTS - 1

    assert not flow.verify(IDENTIFIER, first.code, now=clock.utcnow())
    assert flow.verify(IDENTIFIER, second.code, now=clock.utcnow())


def test_a_missing_digest_is_a_failure(flow, clock, store):
    issued = flow.request(IDENTIFIER, DESTINATION, now=clock.utcnow())
    clock.advance(CODE_TTL)
    assert store.get(f"code:{IDENTIFIER}") is None

    assert not flow.verify(IDENTIFIER, issued.code, now=clock.utcnow())
    assert flow.remaining_attempts(IDENTIFIER, now=clock.utcnow()) == MAX_ATTEMPTS - 1

    again = flow.request(IDENTIFIER, DESTINATION, now=clock.utcnow())
    assert flow.verify(IDENTIFIER, again.code, now=clock.utcnow())


def test_an_unknown_identifier_is_a_failure_too(flow, clock):
    assert not flow.verify("nobody", WRONG, now=clock.utcnow())
    assert flow.remaining_attempts("nobody", now=clock.utcnow()) == MAX_ATTEMPTS - 1


def test_wrong_codes_lock_the_identifier(flow, clock):
    issued = flow.request(IDENTIFIER, DESTINATION, now=clock.utcnow())
    wrong = wrong_code(issued.code)

    for left in (2, 1, 0):
        assert not flow.verify(IDENTIFIER, wrong, now=clock.utcnow())
        assert flow.remaining_attempts(IDENTIFIER, now=clock.utcnow()) == left

    with pytest.raises(LockedOut) as excinfo:
        flow.verify(IDENTIFIER, issued.code, now=clock.utcnow())
    assert excinfo.value.retry_after == LOCKOUT
    assert excinfo.value.retry_after_seconds == pytest.approx(900.0)


def test_the_lock_expires_and_the_tally_starts_over(flow, clock):
    issued = flow.request(IDENTIFIER, DESTINATION, now=clock.utcnow())
    wrong = wrong_code(issued.code)
    for _ in range(MAX_ATTEMPTS):
        assert not flow.verify(IDENTIFIER, wrong, now=clock.utcnow())

    clock.advance(LOCKOUT - timedelta(seconds=1))
    with pytest.raises(LockedOut) as excinfo:
        flow.verify(IDENTIFIER, wrong, now=clock.utcnow())
    assert excinfo.value.retry_after == timedelta(seconds=1)

    clock.advance(timedelta(seconds=1))
    assert flow.remaining_attempts(IDENTIFIER, now=clock.utcnow()) == MAX_ATTEMPTS
    again = flow.request(IDENTIFIER, DESTINATION, now=clock.utcnow())
    assert flow.verify(IDENTIFIER, again.code, now=clock.utcnow())


def test_hammering_a_locked_identifier_does_not_stretch_the_lock(flow, clock, store):
    issued = flow.request(IDENTIFIER, DESTINATION, now=clock.utcnow())
    wrong = wrong_code(issued.code)
    for _ in range(MAX_ATTEMPTS):
        flow.verify(IDENTIFIER, wrong, now=clock.utcnow())
    locked = store.get(f"fails:{IDENTIFIER}")

    for _ in range(5):
        clock.advance(timedelta(minutes=1))
        with pytest.raises(LockedOut):
            flow.verify(IDENTIFIER, wrong, now=clock.utcnow())

    assert store.get(f"fails:{IDENTIFIER}") == locked


def test_identifiers_do_not_share_a_cooldown_or_a_tally(flow, clock):
    alice = flow.request("alice", DESTINATION, now=clock.utcnow())
    bob = flow.request("bob", DESTINATION, now=clock.utcnow())

    for _ in range(MAX_ATTEMPTS):
        assert not flow.verify("alice", wrong_code(alice.code), now=clock.utcnow())
    with pytest.raises(LockedOut):
        flow.verify("alice", alice.code, now=clock.utcnow())

    assert flow.remaining_attempts("bob", now=clock.utcnow()) == MAX_ATTEMPTS
    assert flow.verify("bob", bob.code, now=clock.utcnow())


def test_simultaneous_requests_send_one_code(flow, clock, sender):
    outcomes = run_concurrently(
        lambda: flow.request(IDENTIFIER, DESTINATION, now=clock.utcnow())
    )
    issued = [outcome for outcome in outcomes if isinstance(outcome, RequestedCode)]
    refused = [outcome for outcome in outcomes if isinstance(outcome, ResendTooSoon)]
    assert len(issued) == 1
    assert len(refused) == THREADS - 1
    assert len(sender.sent) == 1
    assert flow.verify(IDENTIFIER, issued[0].code, now=clock.utcnow())


def test_only_one_concurrent_verification_spends_a_code(flow, clock, store):
    issued = flow.request(IDENTIFIER, DESTINATION, now=clock.utcnow())
    outcomes = run_concurrently(
        lambda: flow.verify(IDENTIFIER, issued.code, now=clock.utcnow())
    )
    assert outcomes.count(True) == 1
    assert outcomes.count(False) == THREADS - 1
    assert store.get(f"code:{IDENTIFIER}") is None
    assert flow.remaining_attempts(IDENTIFIER, now=clock.utcnow()) == MAX_ATTEMPTS


def test_no_concurrent_submission_slips_past_a_lock(flow, clock, store):
    issued = flow.request(IDENTIFIER, DESTINATION, now=clock.utcnow())
    wrong = wrong_code(issued.code)
    for _ in range(MAX_ATTEMPTS):
        flow.verify(IDENTIFIER, wrong, now=clock.utcnow())

    outcomes = run_concurrently(
        lambda: flow.verify(IDENTIFIER, issued.code, now=clock.utcnow())
    )
    assert all(isinstance(outcome, LockedOut) for outcome in outcomes)
    assert store.get(f"used:{IDENTIFIER}") is None


def test_a_refused_delivery_does_not_start_the_cooldown(store, clock):
    flow = OtpFlow(
        store,
        RefusingSender(),
        code_ttl=CODE_TTL,
        cooldown=ResendCooldown(COOLDOWN),
    )
    for _ in range(2):
        with pytest.raises(DeliveryError):
            flow.request(IDENTIFIER, DESTINATION, now=clock.utcnow())


def test_the_policy_the_pepper_and_the_template_are_used(store, sender, clock):
    policy = CodePolicy(length=8, alphabet=UPPERCASE_UNAMBIGUOUS)
    flow = OtpFlow(
        store,
        sender,
        code_ttl=CODE_TTL,
        policy=policy,
        pepper=b"server-secret",
        template="Your code: {code}",
    )
    issued = flow.request(IDENTIFIER, DESTINATION, now=clock.utcnow())
    assert len(issued.code) == 8
    assert sender.last.body == f"Your code: {issued.code}"

    stored = store.get(f"code:{IDENTIFIER}")
    assert not verify_code(issued.code, stored, policy)
    assert verify_code(issued.code, stored, policy, pepper=b"server-secret")
    assert flow.verify(IDENTIFIER, issued.code.lower(), now=clock.utcnow())


def test_a_universal_code_is_accepted_without_shadowing_a_real_one(
    store, sender, clock
):
    flow = OtpFlow(
        store,
        sender,
        code_ttl=CODE_TTL,
        cooldown=ResendCooldown(COOLDOWN),
        test_mode=Mode(UNIVERSAL, ttl=timedelta(hours=1), now=NOW),
    )
    issued = flow.request(IDENTIFIER, DESTINATION, now=clock.utcnow())
    assert flow.verify(IDENTIFIER, issued.code, now=clock.utcnow())

    clock.advance(COOLDOWN)
    flow.request(IDENTIFIER, DESTINATION, now=clock.utcnow())
    assert flow.verify(IDENTIFIER, UNIVERSAL, now=clock.utcnow())


def test_without_test_mode_a_universal_code_is_just_wrong(flow, clock):
    flow.request(IDENTIFIER, DESTINATION, now=clock.utcnow())
    assert not flow.verify(IDENTIFIER, UNIVERSAL, now=clock.utcnow())


@pytest.mark.parametrize("ttl", [timedelta(0), timedelta(seconds=-1)])
def test_a_non_positive_code_lifetime_is_rejected(store, sender, ttl):
    with pytest.raises(ValueError):
        OtpFlow(store, sender, code_ttl=ttl)


def test_naive_datetimes_are_rejected(flow):
    naive = datetime(2026, 1, 1, 12, 0)
    with pytest.raises(ValueError):
        flow.request(IDENTIFIER, DESTINATION, now=naive)
    with pytest.raises(ValueError):
        flow.verify(IDENTIFIER, WRONG, now=naive)


def test_now_defaults_to_the_current_time(sender):
    flow = OtpFlow(MemoryStorage(), sender, code_ttl=CODE_TTL)
    issued = flow.request(IDENTIFIER, DESTINATION)
    assert flow.remaining_attempts(IDENTIFIER) == 5
    assert flow.verify(IDENTIFIER, issued.code)
