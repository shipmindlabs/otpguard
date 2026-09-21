"""The expiring key/value contract the library keeps its state in."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "DEFAULT_PREFIX",
    "MemoryStorage",
    "RedisStorage",
    "Storage",
]

DEFAULT_PREFIX = "otpguard:"


@runtime_checkable
class Storage(Protocol):
    """A small key/value store whose keys carry their own lifetime.

    Values are text; a counter is text that happens to hold a number. Nothing
    in the library needs more than this, so any store that can expire a key is
    a candidate backend.
    """

    def get(self, key: str) -> str | None:
        """The stored value, or None if the key is absent or has expired."""

    def set(self, key: str, value: str, ttl: timedelta | None = None) -> None:
        """Store a value, replacing what was there, for at most ``ttl``."""

    def incr(self, key: str, ttl: timedelta | None = None) -> int:
        """Add one to a counter and return it, creating it at 1 if absent.

        ``ttl`` applies only when the counter is created, so the window starts
        with the first increment and later ones cannot push it further out.
        """

    def delete(self, key: str) -> None:
        """Remove a key; removing an absent key is not an error."""


class MemoryStorage:
    """An in-process store for tests and single-worker deployments.

    Expired entries are dropped when they are next touched; a key that is never
    read again holds its memory until :meth:`purge` runs.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._entries: dict[str, tuple[str, float | None]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> str | None:
        with self._lock:
            return self._live(key)

    def set(self, key: str, value: str, ttl: timedelta | None = None) -> None:
        expires_at = self._deadline(ttl)
        with self._lock:
            self._entries[key] = (value, expires_at)

    def incr(self, key: str, ttl: timedelta | None = None) -> int:
        deadline = self._deadline(ttl)
        with self._lock:
            current = self._live(key)
            if current is None:
                counter, expires_at = 1, deadline
            else:
                counter = _as_counter(key, current) + 1
                expires_at = self._entries[key][1]
            self._entries[key] = (str(counter), expires_at)
            return counter

    def delete(self, key: str) -> None:
        with self._lock:
            self._entries.pop(key, None)

    def purge(self) -> None:
        """Drop every entry whose lifetime has run out."""
        with self._lock:
            stale = [
                key
                for key, (_, expires_at) in self._entries.items()
                if self._expired(expires_at)
            ]
            for key in stale:
                del self._entries[key]

    def _live(self, key: str) -> str | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if self._expired(expires_at):
            del self._entries[key]
            return None
        return value

    def _expired(self, expires_at: float | None) -> bool:
        return expires_at is not None and expires_at <= self._clock()

    def _deadline(self, ttl: timedelta | None) -> float | None:
        if ttl is None:
            return None
        return self._clock() + _seconds(ttl)


class RedisStorage:
    """Adapter over a synchronous redis-py client.

    Keys are prefixed so the library can share a database with the rest of the
    application. Values come back as text whether or not the client decodes
    responses itself.
    """

    def __init__(self, client: Any, *, prefix: str = DEFAULT_PREFIX) -> None:
        self._client = client
        self._prefix = prefix

    def get(self, key: str) -> str | None:
        raw = self._client.get(self._key(key))
        if raw is None:
            return None
        return raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)

    def set(self, key: str, value: str, ttl: timedelta | None = None) -> None:
        millis = _millis(ttl)
        if millis is None:
            self._client.set(self._key(key), value)
        else:
            self._client.set(self._key(key), value, px=millis)

    def incr(self, key: str, ttl: timedelta | None = None) -> int:
        full = self._key(key)
        counter = int(self._client.incr(full))
        millis = _millis(ttl)
        # Expiring only the freshly created counter keeps the window anchored to
        # the first attempt; re-expiring would let a burst of tries extend it.
        if millis is not None and counter == 1:
            self._client.pexpire(full, millis)
        return counter

    def delete(self, key: str) -> None:
        self._client.delete(self._key(key))

    def _key(self, key: str) -> str:
        return self._prefix + key


def _seconds(ttl: timedelta) -> float:
    seconds = ttl.total_seconds()
    if seconds <= 0:
        raise ValueError("ttl must be positive")
    return seconds


def _millis(ttl: timedelta | None) -> int | None:
    if ttl is None:
        return None
    return math.ceil(_seconds(ttl) * 1000)


def _as_counter(key: str, value: str) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"value at {key!r} is not a counter") from exc
