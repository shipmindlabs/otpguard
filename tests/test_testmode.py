import logging
from datetime import datetime, timedelta, timezone

import pytest

from otpguard import (
    ENVIRONMENT_VAR,
    MAX_TEST_MODE_TTL,
    TEST_MODE_CODE_VAR,
    TEST_MODE_TTL_VAR,
    UPPERCASE_UNAMBIGUOUS,
    CodePolicy,
    hash_code,
    require_no_test_mode,
)

# Aliased: pytest would try to collect anything named TestMode as a test class.
from otpguard import TestMode as Mode
from otpguard import TestModeInProduction as ModeInProduction

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
CODE = "482913"
UNIVERSAL = "000111"
LOGGER_NAME = "otpguard.testmode"


def armed(ttl=timedelta(hours=1)):
    return Mode(UNIVERSAL, ttl=ttl, now=NOW)


def test_test_mode_is_off_unless_it_is_asked_for(caplog):
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        mode = Mode()
    assert not mode.is_configured
    assert not mode.is_active(NOW)
    assert not mode.accepts(UNIVERSAL, NOW)
    assert mode.expires_at is None
    assert caplog.records == []


def test_turning_it_on_is_loud(caplog):
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        mode = armed()
    assert [record.levelno for record in caplog.records] == [logging.CRITICAL]
    message = caplog.records[0].getMessage()
    assert UNIVERSAL not in message
    assert mode.expires_at.isoformat() in message


def test_a_universal_code_cannot_be_set_without_a_lifetime():
    with pytest.raises(ValueError):
        Mode(UNIVERSAL)


def test_a_lifetime_without_a_code_is_rejected():
    with pytest.raises(ValueError):
        Mode(ttl=timedelta(hours=1))


@pytest.mark.parametrize(
    "ttl",
    [
        timedelta(0),
        timedelta(seconds=-1),
        MAX_TEST_MODE_TTL + timedelta(seconds=1),
    ],
)
def test_invalid_lifetimes_are_rejected(ttl):
    with pytest.raises(ValueError):
        Mode(UNIVERSAL, ttl=ttl)


@pytest.mark.parametrize("code", ["", "   ", "-"])
def test_an_empty_universal_code_is_rejected(code):
    with pytest.raises(ValueError):
        Mode(code, ttl=timedelta(hours=1))


def test_the_universal_code_is_accepted_while_it_lasts(caplog):
    mode = armed()
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        assert mode.accepts(UNIVERSAL, NOW)
    assert [record.levelno for record in caplog.records] == [logging.ERROR]
    assert UNIVERSAL not in caplog.records[0].getMessage()


def test_any_other_code_is_not_accepted(caplog):
    mode = armed()
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        assert not mode.accepts(CODE, NOW)
        assert not mode.accepts("", NOW)
    assert caplog.records == []


def test_the_universal_code_expires_and_the_attempt_is_logged(caplog):
    mode = armed()
    assert mode.expires_at == NOW + timedelta(hours=1)
    assert mode.expires_in(NOW + timedelta(minutes=45)) == timedelta(minutes=15)

    later = NOW + timedelta(hours=1)
    assert not mode.is_active(later)
    assert mode.expires_in(later) == timedelta(0)

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        assert not mode.accepts(UNIVERSAL, later)
    assert [record.levelno for record in caplog.records] == [logging.WARNING]


def test_input_is_normalized_like_a_real_code():
    policy = CodePolicy(length=6, alphabet=UPPERCASE_UNAMBIGUOUS)
    mode = Mode("AB2CD3", ttl=timedelta(hours=1), policy=policy, now=NOW)
    assert mode.accepts("ab2-cd3", NOW)
    assert mode.accepts(" AB2 CD3 ", NOW)
    assert not mode.accepts("AB2CD4", NOW)


def test_verify_tries_the_stored_code_first(caplog):
    stored = hash_code(CODE)
    mode = armed()
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        assert mode.verify(CODE, stored, now=NOW)
    assert caplog.records == []
    assert mode.verify(UNIVERSAL, stored, now=NOW)
    assert not mode.verify("123456", stored, now=NOW)


def test_verify_without_test_mode_is_plain_verification():
    mode = Mode()
    stored = hash_code(CODE, pepper=b"server-secret")
    assert mode.verify(CODE, stored, pepper=b"server-secret", now=NOW)
    assert not mode.verify(UNIVERSAL, stored, pepper=b"server-secret", now=NOW)


def test_a_configured_mode_is_refused_where_it_must_not_exist():
    with pytest.raises(ModeInProduction):
        require_no_test_mode(armed())


def test_an_expired_mode_is_refused_too():
    mode = Mode(UNIVERSAL, ttl=timedelta(seconds=60), now=NOW - timedelta(days=1))
    assert not mode.is_active(NOW)
    with pytest.raises(ModeInProduction):
        require_no_test_mode(mode)


def test_a_mode_that_is_off_passes_the_guard():
    mode = Mode()
    assert require_no_test_mode(mode) is mode


def test_from_env_is_off_without_a_code():
    assert not Mode.from_env({}).is_configured
    assert not Mode.from_env({TEST_MODE_CODE_VAR: "  "}).is_configured


def test_from_env_reads_the_code_and_the_lifetime():
    env = {TEST_MODE_CODE_VAR: UNIVERSAL, TEST_MODE_TTL_VAR: "600"}
    mode = Mode.from_env(env, now=NOW)
    assert mode.is_active(NOW)
    assert mode.expires_at == NOW + timedelta(seconds=600)
    assert mode.accepts(UNIVERSAL, NOW)


def test_from_env_refuses_to_arm_a_production_deployment():
    env = {
        TEST_MODE_CODE_VAR: UNIVERSAL,
        TEST_MODE_TTL_VAR: "600",
        ENVIRONMENT_VAR: "Production",
    }
    with pytest.raises(ModeInProduction):
        Mode.from_env(env, now=NOW)


@pytest.mark.parametrize("ttl", ["", "   ", "soon"])
def test_from_env_needs_a_usable_lifetime(ttl):
    env = {TEST_MODE_CODE_VAR: UNIVERSAL, TEST_MODE_TTL_VAR: ttl}
    with pytest.raises(ValueError):
        Mode.from_env(env, now=NOW)


def test_the_repr_keeps_the_universal_code_out_of_logs():
    assert UNIVERSAL not in repr(armed())
    assert repr(Mode()) == "TestMode(off)"


def test_naive_datetimes_are_rejected():
    with pytest.raises(ValueError):
        Mode(UNIVERSAL, ttl=timedelta(hours=1), now=datetime(2026, 1, 1, 12, 0))
    with pytest.raises(ValueError):
        armed().is_active(datetime(2026, 1, 1, 12, 0))


def test_now_defaults_to_the_current_time():
    mode = Mode(UNIVERSAL, ttl=timedelta(seconds=60))
    assert mode.is_active()
    assert mode.accepts(UNIVERSAL)
