"""Quotas: the absolute cap above the cooldown, counted in the store."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from otpguard.storage import Storage

__all__ = [
    "DEFAULT_SCOPE",
    "Quota",
    "QuotaExceeded",
]

DEFAULT_SCOPE = "sends"

ZERO = timedelta(0)


class QuotaExceeded(Exception):
    """Raised when a subject has spent its allowance for the current window."""

    def __init__(
        self, scope: str, subject: str, limit: int, window: timedelta
    ) -> None:
        super().__init__(
            f"{scope} quota for {subject!r} is spent: "
            f"{limit} per {window.total_seconds():.0f}s"
        )
        self.scope = scope
        self.subject = subject
        self.limit = limit
        self.window = window

    @property
    def retry_after(self) -> timedelta:
        """The whole window: how much of it is left is not readable from a counter."""
        return self.window

    @property
    def retry_after_seconds(self) -> float:
        return self.window.total_seconds()


@dataclass(frozen=True)
class Quota:
    """How many times one subject may spend something inside one window.

    A cooldown spaces deliveries out; a quota puts a ceiling on them. The count
    is a single counter in the store under ``<scope>:<subject>``, so the subject
    can be anything worth capping: an identifier, a number prefix, the address a
    request came from.
    """

    limit: int
    window: timedelta
    scope: str = DEFAULT_SCOPE

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise ValueError("limit must be at least 1")
        if self.window <= ZERO:
            raise ValueError("window must be positive")
        if not self.scope.strip():
            raise ValueError("scope must not be empty")

    def key(self, subject: str) -> str:
        """The store key holding this subject's count."""
        if not subject.strip():
            raise ValueError("subject must not be empty")
        return f"{self.scope}:{subject}"

    def used(self, store: Storage, subject: str) -> int:
        """Claims spent in the current window."""
        key = self.key(subject)
        raw = store.get(key)
        if raw is None:
            return 0
        try:
            return int(raw)
        except ValueError as exc:
            raise ValueError(f"value at {key!r} is not a counter") from exc

    def remaining(self, store: Storage, subject: str) -> int:
        """Claims the subject may still spend before it is refused."""
        return max(self.limit - self.used(store, subject), 0)

    def allows(self, store: Storage, subject: str) -> bool:
        return self.used(store, subject) < self.limit

    def check(self, store: Storage, subject: str) -> None:
        """Raise :class:`QuotaExceeded` unless there is room, spending nothing."""
        if not self.allows(store, subject):
            raise QuotaExceeded(self.scope, subject, self.limit, self.window)

    def claim(self, store: Storage, subject: str) -> int:
        """Spend one claim and return the number spent so far.

        The count comes before the verdict, so two requests arriving together
        cannot both find room for the last one. A refused claim still counts,
        which changes nothing: the window is anchored to the claim that created
        the counter.
        """
        spent = store.incr(self.key(subject), self.window)
        if spent > self.limit:
            raise QuotaExceeded(self.scope, subject, self.limit, self.window)
        return spent
