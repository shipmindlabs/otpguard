"""Resend cooldown: how long a caller must wait before asking for a new code."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

__all__ = [
    "DEFAULT_COOLDOWN",
    "ResendCooldown",
    "ResendTooSoon",
]

DEFAULT_COOLDOWN = timedelta(seconds=60)

ZERO = timedelta(0)


class ResendTooSoon(Exception):
    """Raised when a new code is requested before the cooldown has elapsed."""

    def __init__(self, retry_after: timedelta) -> None:
        super().__init__(
            f"a new code can be requested in {retry_after.total_seconds():.1f}s"
        )
        self.retry_after = retry_after

    @property
    def retry_after_seconds(self) -> float:
        return self.retry_after.total_seconds()


@dataclass(frozen=True)
class ResendCooldown:
    """Minimum delay between two code deliveries to the same destination.

    The cooldown itself holds no state: the caller keeps the moment of the last
    delivery next to the pending code and passes it back on the next request.
    """

    interval: timedelta = DEFAULT_COOLDOWN

    def __post_init__(self) -> None:
        if self.interval <= ZERO:
            raise ValueError("interval must be positive")

    def retry_after(
        self, last_sent_at: datetime | None, now: datetime | None = None
    ) -> timedelta:
        """Time left before another code may be sent; zero once it is allowed."""
        if last_sent_at is None:
            return ZERO
        moment = _utcnow() if now is None else _require_aware(now, "now")
        elapsed = moment - _require_aware(last_sent_at, "last_sent_at")
        remaining = self.interval - elapsed
        return remaining if remaining > ZERO else ZERO

    def allows(
        self, last_sent_at: datetime | None, now: datetime | None = None
    ) -> bool:
        return self.retry_after(last_sent_at, now) == ZERO

    def check(
        self, last_sent_at: datetime | None, now: datetime | None = None
    ) -> None:
        """Raise :class:`ResendTooSoon` unless a new code may be sent now."""
        remaining = self.retry_after(last_sent_at, now)
        if remaining > ZERO:
            raise ResendTooSoon(remaining)

    def next_allowed_at(self, last_sent_at: datetime | None) -> datetime | None:
        """The moment the next code may be sent, or None if there is no wait."""
        if last_sent_at is None:
            return None
        return _require_aware(last_sent_at, "last_sent_at") + self.interval


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _require_aware(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value
