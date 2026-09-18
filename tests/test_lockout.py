from datetime import datetime, timedelta, timezone

import pytest

from otpguard import (
    DEFAULT_LOCKOUT,
    DEFAULT_MAX_ATTEMPTS,
    AttemptRecord,
    LockedOut,
    LockoutPolicy,
)

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
POLICY = LockoutPolicy(max_attempts=3, duration=timedelta(minutes=15))


def test_defaults():
    assert DEFAULT_MAX_ATTEMPTS == 5
    assert DEFAULT_LOCKOUT == timedelta(minutes=15)
    policy = LockoutPolicy()
    assert policy.max_attempts == DEFAULT_MAX_ATTEMPTS
    assert policy.duration == DEFAULT_LOCKOUT


def test_an_unknown_identifier_may_try():
    assert POLICY.allows(None)
    assert POLICY.remaining_attempts(None) == 3
    assert POLICY.retry_after(None) == timedelta(0)
    POLICY.check(None)


def test_failures_are_counted_until_the_attempts_run_out():
    record = POLICY.register_failure(None, NOW)
    assert record.failures == 1
    assert POLICY.remaining_attempts(record, NOW) == 2
    assert POLICY.allows(record, NOW)

    record = POLICY.register_failure(record, NOW)
    assert POLICY.remaining_attempts(record, NOW) == 1
    assert POLICY.allows(record, NOW)

    record = POLICY.register_failure(record, NOW)
    assert record.failures == 3
    assert record.locked_until == NOW + timedelta(minutes=15)
    assert not POLICY.allows(record, NOW)
    assert POLICY.remaining_attempts(record, NOW) == 0


def test_check_reports_how_long_the_lock_lasts():
    record = AttemptRecord(failures=3, locked_until=NOW + timedelta(minutes=15))
    with pytest.raises(LockedOut) as excinfo:
        POLICY.check(record, NOW + timedelta(minutes=1))
    assert excinfo.value.retry_after == timedelta(minutes=14)
    assert excinfo.value.retry_after_seconds == pytest.approx(840.0)


def test_a_single_attempt_policy_locks_immediately():
    policy = LockoutPolicy(max_attempts=1, duration=timedelta(minutes=5))
    record = policy.register_failure(None, NOW)
    assert not policy.allows(record, NOW)
    assert policy.retry_after(record, NOW) == timedelta(minutes=5)


def test_the_lock_expires_and_the_tally_starts_over():
    record = AttemptRecord(failures=3, locked_until=NOW + timedelta(minutes=15))
    later = NOW + timedelta(minutes=15)
    assert POLICY.allows(record, later)
    assert POLICY.remaining_attempts(record, later) == 3
    assert POLICY.register_failure(record, later) == AttemptRecord(failures=1)


def test_attempts_while_locked_do_not_extend_the_lock():
    locked = AttemptRecord(failures=3, locked_until=NOW + timedelta(minutes=15))
    assert POLICY.register_failure(locked, NOW + timedelta(minutes=5)) == locked


def test_success_clears_the_tally():
    assert POLICY.register_success() == AttemptRecord()
    assert POLICY.remaining_attempts(POLICY.register_success(), NOW) == 3


def test_a_lock_survives_a_restart():
    record = AttemptRecord(failures=3, locked_until=NOW + timedelta(minutes=15))
    stored = record.encode()

    restored = AttemptRecord.decode(stored)
    assert restored == record
    assert not POLICY.allows(restored, NOW)
    assert POLICY.retry_after(restored, NOW) == timedelta(minutes=15)


def test_an_encoded_record_is_accepted_directly():
    stored = POLICY.register_failure(None, NOW).encode()
    assert stored == '{"failures":1}'
    assert POLICY.remaining_attempts(stored, NOW) == 2
    assert POLICY.register_failure(stored, NOW).failures == 2


def test_encoded_form_keeps_the_offset_as_an_instant():
    locked_until = (NOW + timedelta(minutes=15)).astimezone(
        timezone(timedelta(hours=5))
    )
    record = AttemptRecord(failures=3, locked_until=locked_until)
    assert POLICY.retry_after(record.encode(), NOW) == timedelta(minutes=15)


def test_str_is_the_encoded_form():
    record = AttemptRecord(failures=2)
    assert str(record) == record.encode()


@pytest.mark.parametrize(
    "encoded",
    [
        "nonsense",
        "[]",
        '{"failures":"two"}',
        '{"failures":1,"locked_until":17}',
        '{"failures":1,"locked_until":"not-a-date"}',
        '{"failures":1,"locked_until":"2026-01-01T12:00:00"}',
    ],
)
def test_decode_rejects_a_malformed_record(encoded):
    with pytest.raises(ValueError):
        AttemptRecord.decode(encoded)


def test_negative_failures_are_rejected():
    with pytest.raises(ValueError):
        AttemptRecord(failures=-1)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_attempts": 0},
        {"max_attempts": -1},
        {"duration": timedelta(0)},
        {"duration": timedelta(seconds=-1)},
    ],
)
def test_invalid_policies_are_rejected(kwargs):
    with pytest.raises(ValueError):
        LockoutPolicy(**kwargs)


def test_naive_datetimes_are_rejected():
    record = AttemptRecord(failures=3, locked_until=NOW)
    with pytest.raises(ValueError):
        AttemptRecord(failures=3, locked_until=datetime(2026, 1, 1, 12, 0))
    with pytest.raises(ValueError):
        POLICY.retry_after(record, datetime(2026, 1, 1, 12, 0))


def test_now_defaults_to_the_current_time():
    soon = datetime.now(timezone.utc) + timedelta(minutes=1)
    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert not POLICY.allows(AttemptRecord(failures=3, locked_until=soon))
    assert POLICY.allows(AttemptRecord(failures=3, locked_until=past))
