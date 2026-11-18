from datetime import datetime, timedelta, timezone

import pytest

from otpguard import (
    DeliveryError,
    LockedOut,
    LockoutPolicy,
    MemoryStorage,
    Message,
    OtpFlow,
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


class FlakySender:
    """A channel that refuses every message until it is told to stop."""

    def __init__(self) -> None:
        self.fail = True
        self.offered: list[str] = []

    def send(self, message: Message) -> None:
        self.offered.append(message.code)
        if self.fail:
            raise DeliveryError("the gateway refused the message")


def wrong_code(code: str) -> str:
    """A candidate that is certainly not the issued code."""
    return WRONG if code != WRONG else "111111"


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


def armed_flow(store, sender):
    """A flow whose universal code is configured and has not run out."""
    return OtpFlow(
        store,
        sender,
        code_ttl=CODE_TTL,
        cooldown=ResendCooldown(COOLDOWN),
        lockout=LockoutPolicy(max_attempts=MAX_ATTEMPTS, duration=LOCKOUT),
        test_mode=Mode(UNIVERSAL, ttl=timedelta(hours=1), now=NOW),
    )


def test_a_malformed_digest_is_a_fault_not_a_wrong_code(flow, clock, store):
    store.set(f"code:{IDENTIFIER}", "nonsense", CODE_TTL)
    with pytest.raises(ValueError):
        flow.verify(IDENTIFIER, WRONG, now=clock.utcnow())
    assert flow.remaining_attempts(IDENTIFIER, now=clock.utcnow()) == MAX_ATTEMPTS


def test_a_malformed_tally_is_a_fault_too(flow, clock, store):
    store.set(f"fails:{IDENTIFIER}", "{", CODE_TTL)
    with pytest.raises(ValueError):
        flow.verify(IDENTIFIER, WRONG, now=clock.utcnow())
    with pytest.raises(ValueError):
        flow.remaining_attempts(IDENTIFIER, now=clock.utcnow())


def test_a_gate_that_is_not_a_counter_sends_nothing(flow, clock, store, sender):
    store.set(f"gate:{IDENTIFIER}", "soon", COOLDOWN)
    with pytest.raises(ValueError):
        flow.request(IDENTIFIER, DESTINATION, now=clock.utcnow())
    assert sender.sent == ()


def test_a_spend_claim_that_is_not_a_counter_is_a_fault(flow, clock, store):
    issued = flow.request(IDENTIFIER, DESTINATION, now=clock.utcnow())
    store.set(f"used:{IDENTIFIER}", "spent", CODE_TTL)
    with pytest.raises(ValueError):
        flow.verify(IDENTIFIER, issued.code, now=clock.utcnow())


def test_an_unusable_destination_delivers_nothing(flow, clock, sender):
    with pytest.raises(ValueError):
        flow.request(IDENTIFIER, "   ", now=clock.utcnow())
    assert sender.sent == ()


def test_a_refused_delivery_leaves_the_next_request_free(store, clock):
    sender = FlakySender()
    flow = OtpFlow(
        store, sender, code_ttl=CODE_TTL, cooldown=ResendCooldown(COOLDOWN)
    )
    with pytest.raises(DeliveryError):
        flow.request(IDENTIFIER, DESTINATION, now=clock.utcnow())

    sender.fail = False
    issued = flow.request(IDENTIFIER, DESTINATION, now=clock.utcnow())
    assert len(sender.offered) == 2
    assert verify_code(issued.code, store.get(f"code:{IDENTIFIER}"))
    assert flow.verify(IDENTIFIER, issued.code, now=clock.utcnow())


def test_a_gate_outliving_its_timestamp_reports_the_whole_wait(flow, clock, store):
    flow.request(IDENTIFIER, DESTINATION, now=clock.utcnow())

    store.delete(f"sent:{IDENTIFIER}")
    with pytest.raises(ResendTooSoon) as excinfo:
        flow.request(IDENTIFIER, DESTINATION, now=clock.utcnow())
    assert excinfo.value.retry_after == COOLDOWN

    stale = (NOW - timedelta(hours=1)).isoformat()
    store.set(f"sent:{IDENTIFIER}", stale, COOLDOWN)
    with pytest.raises(ResendTooSoon) as excinfo:
        flow.request(IDENTIFIER, DESTINATION, now=clock.utcnow())
    assert excinfo.value.retry_after == COOLDOWN


def test_a_locked_identifier_can_still_be_sent_a_code(flow, clock, sender):
    issued = flow.request(IDENTIFIER, DESTINATION, now=clock.utcnow())
    wrong = wrong_code(issued.code)
    for _ in range(MAX_ATTEMPTS):
        flow.verify(IDENTIFIER, wrong, now=clock.utcnow())

    clock.advance(COOLDOWN)
    again = flow.request(IDENTIFIER, DESTINATION, now=clock.utcnow())
    assert len(sender.sent) == 2
    assert flow.remaining_attempts(IDENTIFIER, now=clock.utcnow()) == 0
    with pytest.raises(LockedOut):
        flow.verify(IDENTIFIER, again.code, now=clock.utcnow())


def test_the_second_lock_through_the_flow_lasts_longer(store, sender, clock):
    flow = OtpFlow(
        store,
        sender,
        code_ttl=CODE_TTL,
        cooldown=ResendCooldown(COOLDOWN),
        lockout=LockoutPolicy(
            max_attempts=2,
            duration=timedelta(minutes=1),
            escalation=2,
            max_duration=timedelta(minutes=10),
        ),
    )
    for _ in range(2):
        assert not flow.verify(IDENTIFIER, WRONG, now=clock.utcnow())
    with pytest.raises(LockedOut) as excinfo:
        flow.verify(IDENTIFIER, WRONG, now=clock.utcnow())
    assert excinfo.value.retry_after == timedelta(minutes=1)

    clock.advance(timedelta(minutes=1))
    for _ in range(2):
        assert not flow.verify(IDENTIFIER, WRONG, now=clock.utcnow())
    with pytest.raises(LockedOut) as excinfo:
        flow.verify(IDENTIFIER, WRONG, now=clock.utcnow())
    assert excinfo.value.retry_after == timedelta(minutes=2)

    clock.advance(timedelta(minutes=1, seconds=30))
    with pytest.raises(LockedOut) as excinfo:
        flow.verify(IDENTIFIER, WRONG, now=clock.utcnow())
    assert excinfo.value.retry_after == timedelta(seconds=30)


def test_a_spent_code_becomes_a_failure_once_the_claim_expires(flow, clock):
    issued = flow.request(IDENTIFIER, DESTINATION, now=clock.utcnow())
    assert flow.verify(IDENTIFIER, issued.code, now=clock.utcnow())

    clock.advance(CODE_TTL)
    assert not flow.verify(IDENTIFIER, issued.code, now=clock.utcnow())
    assert flow.remaining_attempts(IDENTIFIER, now=clock.utcnow()) == MAX_ATTEMPTS - 1


def test_separators_in_a_submitted_code_are_folded(flow, clock):
    issued = flow.request(IDENTIFIER, DESTINATION, now=clock.utcnow())
    assert flow.verify(IDENTIFIER, " ".join(issued.code), now=clock.utcnow())


def test_a_universal_code_spends_the_pending_code(store, sender, clock):
    flow = armed_flow(store, sender)
    issued = flow.request(IDENTIFIER, DESTINATION, now=clock.utcnow())

    assert flow.verify(IDENTIFIER, UNIVERSAL, now=clock.utcnow())
    assert store.get(f"code:{IDENTIFIER}") is None
    assert not flow.verify(IDENTIFIER, issued.code, now=clock.utcnow())
    assert not flow.verify(IDENTIFIER, UNIVERSAL, now=clock.utcnow())
    assert flow.remaining_attempts(IDENTIFIER, now=clock.utcnow()) == MAX_ATTEMPTS


def test_a_universal_code_cannot_rescue_an_expired_one(store, sender, clock):
    flow = armed_flow(store, sender)
    flow.request(IDENTIFIER, DESTINATION, now=clock.utcnow())

    clock.advance(CODE_TTL)
    assert not flow.verify(IDENTIFIER, UNIVERSAL, now=clock.utcnow())
    assert flow.remaining_attempts(IDENTIFIER, now=clock.utcnow()) == MAX_ATTEMPTS - 1


def test_identifiers_that_look_like_keys_stay_separate(flow, clock):
    alice = flow.request("alice", DESTINATION, now=clock.utcnow())
    shadow = flow.request("code:alice", DESTINATION, now=clock.utcnow())

    assert not flow.verify("code:alice", wrong_code(shadow.code), now=clock.utcnow())
    assert flow.remaining_attempts("alice", now=clock.utcnow()) == MAX_ATTEMPTS
    assert flow.verify("code:alice", shadow.code, now=clock.utcnow())
    assert flow.verify("alice", alice.code, now=clock.utcnow())


def test_remaining_attempts_rejects_a_naive_datetime(flow):
    with pytest.raises(ValueError):
        flow.remaining_attempts(IDENTIFIER, now=datetime(2026, 1, 1, 12, 0))
