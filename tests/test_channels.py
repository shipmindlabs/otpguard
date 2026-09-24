import logging

import pytest

from otpguard import (
    DEFAULT_TEMPLATE,
    DeliveryError,
    Message,
    Sender,
    StubSender,
    StubSenderInProduction,
    require_real_sender,
)

CODE = "482913"
PHONE = "+15551234567"


class Recorder:
    """A sender that behaves like a real channel, successful or not."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.delivered: list[str] = []

    def send(self, message: Message) -> None:
        if self.fail:
            raise DeliveryError("the gateway refused the message")
        self.delivered.append(message.body)


def test_senders_satisfy_the_protocol():
    assert isinstance(StubSender(), Sender)
    assert isinstance(Recorder(), Sender)


def test_the_body_is_rendered_from_the_template():
    message = Message(PHONE, CODE)
    assert message.body == DEFAULT_TEMPLATE.format(code=CODE)
    assert CODE in message.body


def test_a_template_can_be_supplied():
    message = Message("alice@example.com", CODE, template="Your code: {code}")
    assert message.body == "Your code: 482913"


@pytest.mark.parametrize(
    "override",
    [
        {"destination": ""},
        {"destination": "   "},
        {"code": ""},
        {"template": "no placeholder here"},
    ],
)
def test_invalid_messages_are_rejected(override):
    fields = {"destination": PHONE, "code": CODE, **override}
    with pytest.raises(ValueError):
        Message(**fields)


def test_the_repr_keeps_the_code_out_of_logs():
    message = Message(PHONE, CODE)
    assert CODE not in repr(message)
    assert CODE not in str(message)
    assert PHONE in repr(message)


def test_the_stub_keeps_messages_instead_of_delivering_them():
    sender = StubSender()
    message = Message(PHONE, CODE)
    sender.send(message)
    assert sender.sent == (message,)
    assert sender.last is message
    assert sender.last.body == DEFAULT_TEMPLATE.format(code=CODE)


def test_the_stub_can_be_emptied():
    sender = StubSender()
    sender.send(Message(PHONE, CODE))
    sender.clear()
    assert sender.sent == ()
    assert sender.last is None


def test_the_stub_warns_about_every_message(caplog):
    with caplog.at_level(logging.WARNING, logger="otpguard.channels"):
        StubSender().send(Message(PHONE, CODE))
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelno == logging.WARNING
    assert PHONE in record.getMessage()
    assert CODE not in record.getMessage()


def test_the_warning_can_be_routed_to_another_logger(caplog):
    with caplog.at_level(logging.WARNING, logger="app.otp"):
        StubSender(logger=logging.getLogger("app.otp")).send(Message(PHONE, CODE))
    assert [record.name for record in caplog.records] == ["app.otp"]


def test_a_stub_is_refused_where_a_real_sender_is_required():
    with pytest.raises(StubSenderInProduction) as excinfo:
        require_real_sender(StubSender())
    assert "StubSender" in str(excinfo.value)


def test_a_real_sender_passes_the_guard():
    sender = Recorder()
    assert require_real_sender(sender) is sender
    sender.send(Message(PHONE, CODE))
    assert sender.delivered == [DEFAULT_TEMPLATE.format(code=CODE)]


def test_a_failing_channel_raises_delivery_error():
    with pytest.raises(DeliveryError):
        Recorder(fail=True).send(Message(PHONE, CODE))
