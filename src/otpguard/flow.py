"""The flow: storage, cooldown, lockout and a delivery channel as one object."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from otpguard.channels import DEFAULT_TEMPLATE, DeliveryError, Message, Sender
from otpguard.codes import DEFAULT_POLICY, CodePolicy, generate_code, hash_code
from otpguard.cooldown import ResendCooldown, ResendTooSoon
from otpguard.lockout import LockoutPolicy
from otpguard.storage import Storage
from otpguard.testmode import TestMode

__all__ = [
    "OtpFlow",
    "RequestedCode",
]

ZERO = timedelta(0)


@dataclass(frozen=True, repr=False)
class RequestedCode:
    """What a caller gets back once a code has been delivered."""

    identifier: str
    destination: str
    code: str
    expires_at: datetime
    resend_allowed_at: datetime

    def __repr__(self) -> str:
        # A live code in a log line or a traceback is a code someone else can use.
        return (
            f"RequestedCode(identifier={self.identifier!r}, "
            f"destination={self.destination!r}, code='***', "
            f"expires_at={self.expires_at!r})"
        )


class OtpFlow:
    """Requesting and spending a one-time code, with the rules already wired.

    The flow keeps nothing in the process: the pending digest, the resend claim,
    the moment of the last delivery, the spend claim and the attempt tally are
    five keys in the store, so any number of workers can share one flow.
    """

    def __init__(
        self,
        store: Storage,
        sender: Sender,
        *,
        code_ttl: timedelta,
        cooldown: ResendCooldown | None = None,
        lockout: LockoutPolicy | None = None,
        policy: CodePolicy = DEFAULT_POLICY,
        test_mode: TestMode | None = None,
        pepper: bytes | str = b"",
        template: str = DEFAULT_TEMPLATE,
    ) -> None:
        if code_ttl <= ZERO:
            raise ValueError("code_ttl must be positive")
        self.store = store
        self.sender = sender
        self.code_ttl = code_ttl
        self.cooldown = ResendCooldown() if cooldown is None else cooldown
        self.lockout = LockoutPolicy() if lockout is None else lockout
        self.policy = policy
        self.test_mode = TestMode(policy=policy) if test_mode is None else test_mode
        self.pepper = pepper
        self.template = template

    def request(
        self,
        identifier: str,
        destination: str,
        *,
        now: datetime | None = None,
    ) -> RequestedCode:
        """Draw a code, store its digest and hand it to the channel.

        Raises :class:`ResendTooSoon` while the cooldown is running, and lets a
        :class:`DeliveryError` from the channel through once the resend claim
        has been released.
        """
        moment = _utcnow() if now is None else _require_aware(now, "now")
        interval = self.cooldown.interval
        gate = self._key("gate", identifier)
        # The gate is a counter rather than a timestamp: whoever creates it owns
        # the send, so two requests arriving together still cost one message.
        if self.store.incr(gate, interval) != 1:
            raise ResendTooSoon(self._retry_after(identifier, moment))
        code = generate_code(self.policy)
        hashed = hash_code(code, self.policy, pepper=self.pepper)
        sent = self._key("sent", identifier)
        self.store.set(self._key("code", identifier), hashed.encode(), self.code_ttl)
        self.store.set(sent, moment.isoformat(), interval)
        self.store.delete(self._key("used", identifier))
        # The tally is deliberately untouched: a resend is not an attempt reset.
        try:
            self.sender.send(Message(destination, code, self.template))
        except DeliveryError:
            self.store.delete(gate)
            self.store.delete(sent)
            raise
        return RequestedCode(
            identifier=identifier,
            destination=destination,
            code=code,
            expires_at=moment + self.code_ttl,
            resend_allowed_at=moment + interval,
        )

    def verify(
        self,
        identifier: str,
        candidate: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Spend the pending code, or charge the attempt to the tally.

        Raises :class:`LockedOut` while the identifier is locked. A submission
        with no pending digest is a failure rather than an error; the only one
        that costs no attempt is a repeat of a success.
        """
        moment = _utcnow() if now is None else _require_aware(now, "now")
        fails = self._key("fails", identifier)
        record = self.store.get(fails)
        self.lockout.check(record, moment)
        code_key = self._key("code", identifier)
        used = self._key("used", identifier)
        stored = self.store.get(code_key)
        if stored is not None and self.test_mode.verify(
            candidate, stored, pepper=self.pepper, now=moment
        ):
            # One code, one success: the increment is the part that must be atomic.
            if self.store.incr(used, self.code_ttl) != 1:
                return False
            self.store.delete(code_key)
            self.store.delete(fails)
            return True
        if stored is None and self.store.get(used) is not None:
            return False
        self.store.set(
            fails,
            self.lockout.register_failure(record, moment).encode(),
            self.lockout.duration + self.code_ttl,
        )
        return False

    def remaining_attempts(
        self, identifier: str, *, now: datetime | None = None
    ) -> int:
        """Wrong codes the identifier may still submit before it is locked."""
        record = self.store.get(self._key("fails", identifier))
        return self.lockout.remaining_attempts(record, now)

    def _retry_after(self, identifier: str, now: datetime) -> timedelta:
        raw = self.store.get(self._key("sent", identifier))
        remaining = (
            ZERO
            if raw is None
            else self.cooldown.retry_after(datetime.fromisoformat(raw), now)
        )
        # The gate can outlive the timestamp when the store's clock and ``now``
        # disagree; report the whole wait rather than none at all.
        return remaining if remaining > ZERO else self.cooldown.interval

    def _key(self, kind: str, identifier: str) -> str:
        return f"{kind}:{identifier}"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _require_aware(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value
