"""Delivery channels: the contract a sender meets, and a stub to develop against."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol, TypeVar, runtime_checkable

__all__ = [
    "DEFAULT_TEMPLATE",
    "DeliveryError",
    "Message",
    "Sender",
    "StubSender",
    "StubSenderInProduction",
    "require_real_sender",
]

DEFAULT_TEMPLATE = "{code} is your verification code."

LOGGER = logging.getLogger(__name__)


class DeliveryError(Exception):
    """Raised by a sender when a channel refuses or fails to take a message."""


class StubSenderInProduction(RuntimeError):
    """Raised when a sender that delivers nothing is used where it matters."""

    def __init__(self, sender: object) -> None:
        super().__init__(
            f"{type(sender).__name__} delivers nothing; configure a real sender"
        )


@dataclass(frozen=True, repr=False)
class Message:
    """One code on its way to one destination."""

    destination: str
    code: str
    template: str = DEFAULT_TEMPLATE

    def __post_init__(self) -> None:
        if not self.destination.strip():
            raise ValueError("destination must not be empty")
        if not self.code:
            raise ValueError("code must not be empty")
        if "{code}" not in self.template:
            raise ValueError("template must contain {code}")

    @property
    def body(self) -> str:
        """The text to hand to the channel."""
        return self.template.format(code=self.code)

    def __repr__(self) -> str:
        # A live code in a log line or a traceback is a code someone else can use.
        return f"Message(destination={self.destination!r}, code='***')"


@runtime_checkable
class Sender(Protocol):
    """Somewhere a code can be delivered to.

    A channel is one verb. Anything that can take a :class:`Message` and put it
    in front of a person is a sender, so an application can swap SMS for email
    or for its own gateway without the library knowing.
    """

    def send(self, message: Message) -> None:
        """Deliver the message, raising :class:`DeliveryError` if the channel fails."""


class StubSender:
    """A sender for development that keeps messages instead of delivering them.

    Nothing leaves the process, so tests and local runs can read the code back.
    Every message is logged at WARNING and the sender admits to being a stub, so
    one that reaches a deployment is noisy rather than silent.
    """

    is_stub = True

    def __init__(self, *, logger: logging.Logger | None = None) -> None:
        self._logger = LOGGER if logger is None else logger
        self._sent: list[Message] = []

    def send(self, message: Message) -> None:
        self._sent.append(message)
        self._logger.warning(
            "stub sender kept a code for %s; nothing was delivered",
            message.destination,
        )

    @property
    def sent(self) -> tuple[Message, ...]:
        """Every message handed to the stub, oldest first."""
        return tuple(self._sent)

    @property
    def last(self) -> Message | None:
        return self._sent[-1] if self._sent else None

    def clear(self) -> None:
        self._sent.clear()


SenderT = TypeVar("SenderT", bound=Sender)


def require_real_sender(sender: SenderT) -> SenderT:
    """Return the sender unless it only pretends to deliver.

    Call this where an application wires its channels together: a stub left in a
    deployment accepts every code and delivers none of them.
    """
    if getattr(sender, "is_stub", False):
        raise StubSenderInProduction(sender)
    return sender
