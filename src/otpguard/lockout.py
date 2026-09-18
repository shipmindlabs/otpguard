"""Attempt counting and lockout after too many wrong codes."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

__all__ = [
    "DEFAULT_LOCKOUT",
    "DEFAULT_MAX_ATTEMPTS",
    "AttemptRecord",
    "LockedOut",
    "LockoutPolicy",
]

DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_LOCKOUT = timedelta(minutes=15)

ZERO = timedelta(0)


class LockedOut(Exception):
    """Raised when an identifier has spent its attempts and is still locked."""

    def __init__(self, retry_after: timedelta) -> None:
        super().__init__(
            f"too many wrong codes; retry in {retry_after.total_seconds():.1f}s"
        )
        self.retry_after = retry_after

    @property
    def retry_after_seconds(self) -> float:
        return self.retry_after.total_seconds()


@dataclass(frozen=True)
class AttemptRecord:
    """Wrong-code tally for a single identifier.

    The record is the entire state of a lockout: persist :meth:`encode` next to
    the pending code and the lock outlives the process that created it.
    """

    failures: int = 0
    locked_until: datetime | None = None

    def __post_init__(self) -> None:
        if self.failures < 0:
            raise ValueError("failures must not be negative")
        if self.locked_until is not None:
            _require_aware(self.locked_until, "locked_until")

    def encode(self) -> str:
        payload: dict[str, object] = {"failures": self.failures}
        if self.locked_until is not None:
            payload["locked_until"] = self.locked_until.isoformat()
        return json.dumps(payload, separators=(",", ":"))

    @classmethod
    def decode(cls, encoded: str) -> AttemptRecord:
        try:
            payload = json.loads(encoded)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("malformed attempt record") from exc
        if not isinstance(payload, dict):
            raise ValueError("malformed attempt record")
        failures = payload.get("failures", 0)
        if not isinstance(failures, int) or isinstance(failures, bool):
            raise ValueError("malformed attempt record")
        raw = payload.get("locked_until")
        if raw is None:
            locked_until = None
        elif isinstance(raw, str):
            try:
                locked_until = datetime.fromisoformat(raw)
            except ValueError as exc:
                raise ValueError("malformed attempt record") from exc
        else:
            raise ValueError("malformed attempt record")
        return cls(failures=failures, locked_until=locked_until)

    def __str__(self) -> str:
        return self.encode()


@dataclass(frozen=True)
class LockoutPolicy:
    """How many wrong codes an identifier may submit before it has to wait.

    The policy holds no state: the caller keeps an :class:`AttemptRecord` per
    identifier and passes it back on every verification.
    """

    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    duration: timedelta = DEFAULT_LOCKOUT

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.duration <= ZERO:
            raise ValueError("duration must be positive")

    def retry_after(
        self,
        record: AttemptRecord | str | None,
        now: datetime | None = None,
    ) -> timedelta:
        """Time left before another code may be submitted; zero while free."""
        moment = _utcnow() if now is None else _require_aware(now, "now")
        current = self._current(record, moment)
        if current.locked_until is None:
            return ZERO
        return current.locked_until - moment

    def allows(
        self,
        record: AttemptRecord | str | None,
        now: datetime | None = None,
    ) -> bool:
        return self.retry_after(record, now) == ZERO

    def check(
        self,
        record: AttemptRecord | str | None,
        now: datetime | None = None,
    ) -> None:
        """Raise :class:`LockedOut` unless a code may be submitted now."""
        remaining = self.retry_after(record, now)
        if remaining > ZERO:
            raise LockedOut(remaining)

    def remaining_attempts(
        self,
        record: AttemptRecord | str | None,
        now: datetime | None = None,
    ) -> int:
        """Wrong codes still allowed before the identifier is locked."""
        moment = _utcnow() if now is None else _require_aware(now, "now")
        current = self._current(record, moment)
        if current.locked_until is not None:
            return 0
        return max(self.max_attempts - current.failures, 0)

    def register_failure(
        self,
        record: AttemptRecord | str | None = None,
        now: datetime | None = None,
    ) -> AttemptRecord:
        """Count a wrong code, locking the identifier once attempts run out.

        A submission made while the identifier is already locked leaves the
        record untouched, so hammering the endpoint cannot stretch the lockout.
        """
        moment = _utcnow() if now is None else _require_aware(now, "now")
        current = self._current(record, moment)
        if current.locked_until is not None:
            return current
        failures = current.failures + 1
        locked = failures >= self.max_attempts
        return AttemptRecord(
            failures=failures,
            locked_until=moment + self.duration if locked else None,
        )

    def register_success(self) -> AttemptRecord:
        """The cleared record to store once a code has been accepted."""
        return AttemptRecord()

    def _current(
        self, record: AttemptRecord | str | None, now: datetime
    ) -> AttemptRecord:
        if record is None:
            return AttemptRecord()
        if isinstance(record, str):
            record = AttemptRecord.decode(record)
        if record.locked_until is not None and record.locked_until <= now:
            return AttemptRecord()
        return record


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _require_aware(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value
