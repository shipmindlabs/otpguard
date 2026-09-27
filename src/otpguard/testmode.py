"""Test mode: an explicit, expiring universal code that is off unless asked for."""

from __future__ import annotations

import hmac
import logging
import os
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone

from otpguard.codes import DEFAULT_POLICY, CodePolicy, HashedCode, verify_code

__all__ = [
    "ENVIRONMENT_VAR",
    "MAX_TEST_MODE_TTL",
    "PRODUCTION_ENVIRONMENTS",
    "TEST_MODE_CODE_VAR",
    "TEST_MODE_TTL_VAR",
    "TestMode",
    "TestModeInProduction",
    "require_no_test_mode",
]

#: Whatever the caller asks for, a universal code cannot outlive a working day.
MAX_TEST_MODE_TTL = timedelta(hours=24)

TEST_MODE_CODE_VAR = "OTPGUARD_TEST_MODE_CODE"
TEST_MODE_TTL_VAR = "OTPGUARD_TEST_MODE_TTL"
ENVIRONMENT_VAR = "OTPGUARD_ENV"
PRODUCTION_ENVIRONMENTS = frozenset({"live", "prod", "production"})

LOGGER = logging.getLogger(__name__)

ZERO = timedelta(0)


class TestModeInProduction(RuntimeError):
    """Raised when a universal code is configured where it must not exist."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"test mode must not be configured here: {reason}")


class TestMode:
    """A universal code for development, and the rules that keep it contained.

    A code that opens every account is the one thing this library must never
    carry by default, so the mode is off until a code is passed, a code cannot
    be passed without a lifetime, and the lifetime cannot exceed
    :data:`MAX_TEST_MODE_TTL`. Turning it on logs at CRITICAL and every accepted
    universal code logs at ERROR, so a deployment that kept it cannot be quiet.
    """

    def __init__(
        self,
        universal_code: str | None = None,
        *,
        ttl: timedelta | None = None,
        policy: CodePolicy = DEFAULT_POLICY,
        now: datetime | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._logger = LOGGER if logger is None else logger
        self._policy = policy
        self._universal: bytes | None = None
        self.expires_at: datetime | None = None
        if universal_code is None:
            if ttl is not None:
                raise ValueError("ttl without a universal code leaves test mode off")
            return
        normalized = policy.normalize(universal_code)
        if not normalized:
            raise ValueError("universal code must not be empty")
        if ttl is None:
            raise ValueError("test mode must expire: pass ttl alongside the code")
        if ttl <= ZERO:
            raise ValueError("ttl must be positive")
        if ttl > MAX_TEST_MODE_TTL:
            raise ValueError(f"ttl must not exceed {MAX_TEST_MODE_TTL}")
        started = _utcnow() if now is None else _require_aware(now, "now")
        self._universal = normalized.encode("utf-8")
        self.expires_at = started + ttl
        self._logger.critical(
            "TEST MODE IS ON: a universal code is accepted for any identifier until %s",
            self.expires_at.isoformat(),
        )

    @property
    def is_configured(self) -> bool:
        """Whether a universal code exists at all, expired or not."""
        return self._universal is not None

    def is_active(self, now: datetime | None = None) -> bool:
        return self.expires_in(now) > ZERO

    def expires_in(self, now: datetime | None = None) -> timedelta:
        """Time the universal code has left; zero when off or expired."""
        if self.expires_at is None:
            return ZERO
        moment = _utcnow() if now is None else _require_aware(now, "now")
        remaining = self.expires_at - moment
        return remaining if remaining > ZERO else ZERO

    def accepts(self, candidate: str, now: datetime | None = None) -> bool:
        """Whether the candidate is the universal code and may still be used."""
        if self._universal is None:
            return False
        offered = self._policy.normalize(candidate).encode("utf-8")
        if not hmac.compare_digest(offered, self._universal):
            return False
        if not self.is_active(now):
            self._logger.warning("an expired universal code was offered and refused")
            return False
        self._logger.error(
            "a universal code was accepted; no stored code was verified"
        )
        return True

    def verify(
        self,
        candidate: str,
        hashed: HashedCode | str,
        *,
        pepper: bytes | str = b"",
        now: datetime | None = None,
    ) -> bool:
        """Verify the stored digest first, and only then the universal code."""
        if verify_code(candidate, hashed, self._policy, pepper=pepper):
            return True
        return self.accepts(candidate, now)

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        policy: CodePolicy = DEFAULT_POLICY,
        now: datetime | None = None,
        logger: logging.Logger | None = None,
    ) -> TestMode:
        """Read test mode from the environment; refuse a production one.

        Without a code the mode is off. With one, a lifetime in seconds is
        mandatory and an environment that calls itself production is refused.
        """
        source = os.environ if env is None else env
        code = (source.get(TEST_MODE_CODE_VAR) or "").strip()
        if not code:
            return cls(policy=policy, logger=logger)
        environment = (source.get(ENVIRONMENT_VAR) or "").strip().lower()
        if environment in PRODUCTION_ENVIRONMENTS:
            raise TestModeInProduction(
                f"{TEST_MODE_CODE_VAR} is set while {ENVIRONMENT_VAR}={environment}"
            )
        raw_ttl = (source.get(TEST_MODE_TTL_VAR) or "").strip()
        if not raw_ttl:
            raise ValueError(
                f"{TEST_MODE_CODE_VAR} requires {TEST_MODE_TTL_VAR} in seconds"
            )
        try:
            seconds = float(raw_ttl)
        except ValueError as exc:
            raise ValueError(
                f"{TEST_MODE_TTL_VAR} must be a number of seconds"
            ) from exc
        return cls(
            code,
            ttl=timedelta(seconds=seconds),
            policy=policy,
            now=now,
            logger=logger,
        )

    def __repr__(self) -> str:
        if self._universal is None:
            return "TestMode(off)"
        return f"TestMode(universal_code='***', expires_at={self.expires_at!r})"


def require_no_test_mode(mode: TestMode) -> TestMode:
    """Return the mode unless it carries a universal code.

    Call this where an application wires itself together: the expiry is a safety
    net for a forgotten code, not a licence to ship one.
    """
    if mode.is_configured:
        raise TestModeInProduction("a universal code is configured")
    return mode


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _require_aware(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value
