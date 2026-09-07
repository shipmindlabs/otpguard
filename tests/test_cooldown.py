from datetime import datetime, timedelta, timezone

import pytest

from otpguard import DEFAULT_COOLDOWN, ResendCooldown, ResendTooSoon

SENT_AT = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def test_default_cooldown_is_one_minute():
    assert DEFAULT_COOLDOWN == timedelta(seconds=60)
    assert ResendCooldown().interval == DEFAULT_COOLDOWN


def test_first_code_is_always_allowed():
    cooldown = ResendCooldown()
    assert cooldown.allows(None)
    assert cooldown.retry_after(None) == timedelta(0)
    cooldown.check(None)


def test_resend_is_blocked_during_the_cooldown():
    cooldown = ResendCooldown(timedelta(seconds=60))
    now = SENT_AT + timedelta(seconds=17)
    assert not cooldown.allows(SENT_AT, now)
    assert cooldown.retry_after(SENT_AT, now) == timedelta(seconds=43)


def test_check_reports_how_long_is_left():
    cooldown = ResendCooldown(timedelta(seconds=60))
    with pytest.raises(ResendTooSoon) as excinfo:
        cooldown.check(SENT_AT, SENT_AT + timedelta(seconds=17))
    assert excinfo.value.retry_after == timedelta(seconds=43)
    assert excinfo.value.retry_after_seconds == pytest.approx(43.0)


def test_resend_is_allowed_once_the_interval_has_passed():
    cooldown = ResendCooldown(timedelta(seconds=60))
    for elapsed in (timedelta(seconds=60), timedelta(seconds=61), timedelta(days=1)):
        now = SENT_AT + elapsed
        assert cooldown.allows(SENT_AT, now)
        assert cooldown.retry_after(SENT_AT, now) == timedelta(0)
        cooldown.check(SENT_AT, now)


def test_next_allowed_at():
    cooldown = ResendCooldown(timedelta(seconds=90))
    assert cooldown.next_allowed_at(SENT_AT) == SENT_AT + timedelta(seconds=90)
    assert cooldown.next_allowed_at(None) is None


def test_offsets_are_compared_as_instants():
    cooldown = ResendCooldown(timedelta(seconds=60))
    sent_at = SENT_AT.astimezone(timezone(timedelta(hours=5)))
    assert not cooldown.allows(sent_at, SENT_AT + timedelta(seconds=30))
    assert cooldown.allows(sent_at, SENT_AT + timedelta(seconds=60))


def test_now_defaults_to_the_current_time():
    cooldown = ResendCooldown(timedelta(seconds=60))
    assert not cooldown.allows(datetime.now(timezone.utc))
    assert cooldown.allows(datetime.now(timezone.utc) - timedelta(minutes=5))


@pytest.mark.parametrize("interval", [timedelta(0), timedelta(seconds=-1)])
def test_non_positive_interval_is_rejected(interval):
    with pytest.raises(ValueError):
        ResendCooldown(interval)


def test_naive_datetimes_are_rejected():
    cooldown = ResendCooldown()
    with pytest.raises(ValueError):
        cooldown.retry_after(datetime(2026, 1, 1, 12, 0))
    with pytest.raises(ValueError):
        cooldown.retry_after(SENT_AT, datetime(2026, 1, 1, 12, 0))
