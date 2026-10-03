# otpguard

Passwordless one-time codes done safely: resend cooldown, attempt counting,
lockout and pluggable delivery channels.

A one-time code is easy to generate and easy to get wrong. otpguard carries the
parts that are usually left to the application: a code drawn from the system
CSPRNG and stored only as a salted digest, a resend cooldown, an attempt tally
that survives a restart, a delivery contract, and a test mode that cannot be
left switched on quietly.

## Status

Early development. The public API is not stable yet.

## Installation

```bash
pip install otpguard
pip install "otpguard[redis]"   # for the redis adapter
```

Python 3.10 or newer. The core library has no dependencies.

## Quickstart

Two entry points: one asks for a code, the other spends it. Every piece of state
lives in a `Storage`, so nothing below keeps state in the process.

```python
from datetime import datetime, timedelta, timezone

from otpguard import (
    LockoutPolicy,
    MemoryStorage,
    Message,
    ResendCooldown,
    StubSender,
    generate_code,
    hash_code,
    verify_code,
)

CODE_TTL = timedelta(minutes=5)

store = MemoryStorage()
cooldown = ResendCooldown(timedelta(seconds=60))
lockout = LockoutPolicy(max_attempts=5, duration=timedelta(minutes=15))
sender = StubSender()          # a real channel goes here in a deployment


def request_code(identifier: str, destination: str) -> None:
    last_sent = store.get(f"sent:{identifier}")
    cooldown.check(datetime.fromisoformat(last_sent) if last_sent else None)

    code = generate_code()
    store.set(f"code:{identifier}", hash_code(code).encode(), CODE_TTL)
    store.set(f"sent:{identifier}", datetime.now(timezone.utc).isoformat(), CODE_TTL)
    sender.send(Message(destination, code))


def submit_code(identifier: str, candidate: str) -> bool:
    lockout.check(store.get(f"fails:{identifier}"))

    stored = store.get(f"code:{identifier}")
    if stored is not None and verify_code(candidate, stored):
        store.delete(f"code:{identifier}")        # one code, one success
        store.delete(f"fails:{identifier}")
        return True

    record = lockout.register_failure(store.get(f"fails:{identifier}"))
    store.set(f"fails:{identifier}", record.encode(), lockout.duration + CODE_TTL)
    return False
```

`cooldown.check` raises `ResendTooSoon` and `lockout.check` raises `LockedOut`;
both carry `retry_after_seconds`, ready for a `Retry-After` header. Removing the
pending digest on success is what makes a code single-use. `tests/test_flow.py`
runs the same flow on an injected clock and uses `Storage.incr` to make that one
step atomic, so two requests arriving together cannot both spend a code.

## The pieces

### Codes

Codes come from the system CSPRNG, are stored as a salted HMAC-SHA256 digest and
are compared in constant time. The clear-text code exists only long enough to be
delivered.

```python
from otpguard import generate_code, hash_code, verify_code

code = generate_code()                  # '482913'
stored = hash_code(code).encode()       # 'hmac-sha256$<salt>$<digest>'

verify_code("482913", stored)           # True
verify_code("000000", stored)           # False
```

Length and alphabet are configurable. The bundled `UPPERCASE_UNAMBIGUOUS`
alphabet drops the characters that are easy to confuse when a code is read aloud
or copied by hand:

```python
from otpguard import UPPERCASE_UNAMBIGUOUS, CodePolicy

policy = CodePolicy(length=8, alphabet=UPPERCASE_UNAMBIGUOUS)
policy.entropy_bits                       # 39.2

code = generate_code(policy)              # 'K7H2MQ4B'
stored = hash_code(code, policy)
verify_code("k7h2-mq4b", stored, policy)  # True, separators and case are folded
```

Pass a `pepper` to bind digests to a secret kept outside the database, so a
leaked table alone cannot be attacked offline:

```python
stored = hash_code(code, pepper=SERVER_SECRET)
verify_code(candidate, stored, pepper=SERVER_SECRET)
```

### Resend cooldown

A second code cannot be requested before the cooldown elapses, and the caller
learns exactly how long is left. The cooldown holds no state itself: store the
moment of the last delivery next to the pending code and pass it back.

```python
from datetime import timedelta
from otpguard import ResendCooldown, ResendTooSoon

cooldown = ResendCooldown(timedelta(seconds=60))

try:
    cooldown.check(last_sent_at)        # None on the first request
except ResendTooSoon as exc:
    exc.retry_after                     # timedelta(seconds=43)
    exc.retry_after_seconds             # 43.0
```

`retry_after()` returns the same remaining time without raising, `allows()`
answers with a bool, and `next_allowed_at()` gives the absolute moment the next
code may go out. Timestamps must be timezone-aware; any offset works.

### Attempt counting and lockout

Wrong codes are counted per identifier: after `max_attempts` of them the
identifier is locked for `duration`. The whole state of a lockout is a small
`AttemptRecord` you persist alongside the pending code, so a redeploy does not
hand an attacker a fresh set of attempts.

```python
from datetime import timedelta
from otpguard import AttemptRecord, LockedOut, LockoutPolicy

lockout = LockoutPolicy(max_attempts=5, duration=timedelta(minutes=15))

try:
    lockout.check(stored_record)             # an encoded record or None
except LockedOut as exc:
    exc.retry_after_seconds                  # 812.4
else:
    if verify_code(candidate, stored):
        record = lockout.register_success()
    else:
        record = lockout.register_failure(stored_record)
        lockout.remaining_attempts(record)   # 2

stored_record = record.encode()              # '{"failures":3}'
```

The encoded record is ASCII JSON and fits in a single column. Once the lock
expires the tally starts over, and attempts made while the identifier is locked
leave the record untouched, so hammering the endpoint cannot stretch the wait.
Methods accept either an `AttemptRecord` or its encoded form.

### Storage

Everything above is deliberately stateless, so the pending digest, the moment of
the last delivery and the attempt tally have to live somewhere. `Storage` is the
contract that somewhere has to meet: `get`, `set`, `incr` and `delete`, with a
lifetime attached to the key.

```python
from datetime import timedelta
from otpguard import MemoryStorage

store = MemoryStorage()
store.set("code:alice", stored, timedelta(minutes=5))
store.get("code:alice")                            # the digest, until it expires
store.incr("fails:alice", timedelta(minutes=15))   # 1, then 2, then 3...
```

`MemoryStorage` lives in the process and suits tests and a single worker. Once
there is more than one, hand the redis adapter a client; keys are prefixed so
the database can be shared with the rest of the application.

```python
import redis
from otpguard import RedisStorage

store = RedisStorage(redis.Redis(), prefix="otpguard:")
```

`incr` attaches the lifetime only when the counter is created, so a burst of
wrong codes cannot push the window further out. Any object with the four methods
will do; `isinstance(store, Storage)` answers whether it has them.

### Delivery channels

`Sender` is the whole contract for a channel: one `send` that takes a `Message`
and raises `DeliveryError` when the channel will not carry it.

```python
from otpguard import DeliveryError, Message

class SmsSender:
    def __init__(self, client):
        self._client = client

    def send(self, message: Message) -> None:
        try:
            self._client.send_sms(message.destination, message.body)
        except TimeoutError as exc:
            raise DeliveryError(str(exc)) from exc
```

`StubSender` stands in while there is no channel yet: it keeps messages in the
process, so a test or a local run can read the code back. A stub that reaches a
deployment accepts every code and delivers none, so each message is logged at
WARNING and the place where the application picks its channel can refuse one
outright.

```python
from otpguard import Message, StubSender, require_real_sender

sender = StubSender()
sender.send(Message("+15551234567", code))
sender.last.body                                      # '482913 is your verification code.'

sender = require_real_sender(build_sender(settings))   # raises on a stub
```

The body comes from a template, `'{code} is your verification code.'` unless you
pass another one, and `Message` keeps the code out of its own repr, so a log line
or a traceback carrying a live message does not hand it out.

### Test mode

A universal code that opens every account is the last thing a one-time code
library should carry by default, so `TestMode` is off unless it is asked for, and
asking takes both a code and the moment it stops working.

```python
from datetime import timedelta
from otpguard import TestMode

mode = TestMode()                                    # off
mode = TestMode("000111", ttl=timedelta(hours=1))    # on, and already expiring
```

`ttl` is required next to the code, must be positive and cannot exceed
`MAX_TEST_MODE_TTL`, which is one day. Switching it on logs at CRITICAL, every
accepted universal code logs at ERROR, and a code offered after it expired is
refused and logged at WARNING. Verification goes through the mode, which tries
the stored digest first, so test mode cannot shadow a real code:

```python
from otpguard import require_no_test_mode

mode.verify(candidate, stored)   # the stored digest, then the test code
mode.accepts(candidate)          # the test code alone

mode = TestMode.from_env()       # OTPGUARD_TEST_MODE_CODE + OTPGUARD_TEST_MODE_TTL
require_no_test_mode(mode)       # raises while a universal code exists at all
```

`from_env` refuses a code outright when `OTPGUARD_ENV` names a production
environment, and `require_no_test_mode` refuses a configured mode whether or not
it has already run out: the expiry is a safety net for a forgotten code, not a
licence to ship one.

## Threat notes

The library closes the holes that live in the code path. The rest of this section
is what it cannot close for you.

### Account enumeration

otpguard never learns whether an identifier exists: it sees a digest, a timestamp
and a tally. That makes enumeration entirely a property of your endpoint, and the
things that leak are the obvious ones:

- **Response shape.** Answer a request for an unknown identifier exactly as you
  answer a known one: same status, same body, same `Retry-After`. "No account
  with that number" is a free list of your users.
- **Timing.** Verification itself is a constant-time digest compare
  (`hmac.compare_digest`), and `TestMode` compares its universal code the same
  way. What leaks instead is the work around it, so avoid skipping the hash for
  a missing identifier and returning early.
- **Cooldown bookkeeping.** Run the cooldown for unknown identifiers too, or the
  presence of a wait becomes the answer.
- **Delivery errors.** A gateway rejecting an address tells you the address is
  wrong; do not pass that distinction to the client.

### Brute force

A six-digit code is 19.9 bits. It is not strong, and it is not meant to be: what
makes it safe is a short life and a hard cap on guesses.

| alphabet | length | combinations | entropy |
| --- | --- | --- | --- |
| `DIGITS` | 6 | 1.0e6 | 19.9 bits |
| `DIGITS` | 8 | 1.0e8 | 26.6 bits |
| `UPPERCASE_UNAMBIGUOUS` | 6 | 7.3e8 | 29.4 bits |
| `UPPERCASE_UNAMBIGUOUS` | 8 | 6.6e11 | 39.2 bits |

With the default policy and `max_attempts=5`, one code window gives an attacker
about a 1-in-200,000 chance. That arithmetic only holds if every one of these is
true:

- **The tally is persisted.** `AttemptRecord.encode()` goes into storage next to
  the pending code. A tally held in memory is a tally an attacker resets by
  waiting for a redeploy or by reaching another worker.
- **The code is single-use.** Delete the digest on success, and treat a submission
  with no pending digest as a failure rather than an error.
- **The code expires.** Give the digest a short TTL (five minutes is typical); the
  library takes the lifetime from you and never defaults it.
- **A resend does not reset the tally.** Replace the digest on resend, leave
  `fails:` alone. Otherwise the resend endpoint is an attempt-counter reset.
- **Attempts while locked stay locked.** `register_failure` returns a locked
  record unchanged, so repeated tries neither extend nor shorten the wait.

Lockout is per identifier. An attacker spreading one guess over many identifiers
is a different shape of attack, and the counter for it is a cap keyed by source
rather than by identifier — `store.incr(f"ip:{address}", timedelta(hours=1))` is
enough to build one.

### SMS cost and pumping

Every delivery costs money, and a request endpoint that sends an SMS to any
number it is given is an endpoint someone can bill you for. Traffic pumping loops
it against ranges that pay the attacker a share of the fee.

- **The cooldown is a floor, not a cap.** Sixty seconds still allows 1440
  messages per identifier per day. Add an absolute cap:
  `store.incr(f"sent:day:{identifier}", timedelta(days=1))` and refuse above a
  threshold you are willing to pay for.
- **Check before you send.** `cooldown.check` belongs ahead of `sender.send`, so
  a refused request costs nothing. Generating the code first is free; sending it
  is not.
- **Cap by destination, not only by identifier.** Counters keyed by country code
  or number prefix catch a flood spread over fresh identifiers, which a
  per-identifier cooldown never sees.
- **Prefer the cheaper channel when you can.** `Sender` is one method, so email
  or push can serve the same flow at a fraction of the cost.
- **Watch the stub.** `StubSender` logs every message at WARNING and
  `require_real_sender` refuses one at startup — a channel that silently delivers
  nothing looks exactly like a channel that works.

### Codes in logs and stores

The clear-text code lives for one function call. `Message.__repr__` and
`TestMode.__repr__` mask it so a traceback or a log line does not hand it out,
and nothing in the library logs a code or a universal code. Keep your own logging
to the same rule, and if you use a pepper, keep it out of the database whose
leak it is meant to survive.

## Configuration reference

No global configuration and no settings file: everything is an argument with a
default.

### Codes

| Setting | Default | What it does |
| --- | --- | --- |
| `CodePolicy.length` | `6` | Characters in a generated code; at least 4 |
| `CodePolicy.alphabet` | `DIGITS` | Characters drawn from; no duplicates, no overlap with separators |
| `CodePolicy.case_sensitive` | `False` | When false, input and stored code are folded to uppercase |
| `CodePolicy.separators` | `DEFAULT_SEPARATORS` | Characters stripped from user input: space, hyphen, tab, carriage return, newline |
| `CodePolicy.entropy_bits` | — | Read-only: `length * log2(len(alphabet))` |
| `hash_code(..., pepper=)` | `b""` | Secret mixed into the digest; must be supplied again to verify |
| `hash_code(..., salt=)` | fresh 16 bytes | Override only to recompute a known digest |
| `DEFAULT_POLICY` | `CodePolicy()` | The policy used when none is passed |
| `ALGORITHM` | `"hmac-sha256"` | Prefix of an encoded digest; another algorithm never verifies |
| `SALT_BYTES` | `16` | Salt length drawn per code |
| `UPPERCASE_UNAMBIGUOUS` | 30 characters | Uppercase letters and digits without `0/O`, `1/I`, `5/S` |

### Cooldown and lockout

| Setting | Default | What it does |
| --- | --- | --- |
| `ResendCooldown.interval` | `DEFAULT_COOLDOWN` (60s) | Minimum delay between two deliveries to one identifier |
| `LockoutPolicy.max_attempts` | `DEFAULT_MAX_ATTEMPTS` (5) | Wrong codes allowed before the identifier is locked |
| `LockoutPolicy.duration` | `DEFAULT_LOCKOUT` (15 min) | How long a locked identifier waits |

Both take an optional `now` on every method for testing, and require
timezone-aware datetimes.

### Storage

| Setting | Default | What it does |
| --- | --- | --- |
| `MemoryStorage(clock=)` | `time.monotonic` | Time source; replace it to test expiry |
| `MemoryStorage.purge()` | — | Drops entries whose lifetime has run out |
| `RedisStorage(client)` | required | Any synchronous redis-py-shaped client |
| `RedisStorage(prefix=)` | `DEFAULT_PREFIX` (`"otpguard:"`) | Key prefix, so the database can be shared |
| `ttl` on `set` / `incr` | `None` | No lifetime at all; the caller picks every TTL |

### Channels

| Setting | Default | What it does |
| --- | --- | --- |
| `Message.template` | `DEFAULT_TEMPLATE` | Body text; must contain `{code}` |
| `StubSender(logger=)` | `otpguard.channels` logger | Where the stub's WARNING per message goes |
| `require_real_sender(sender)` | — | Raises `StubSenderInProduction` for anything with `is_stub` |

### Test mode

| Setting | Default | What it does |
| --- | --- | --- |
| `TestMode(universal_code)` | `None` | Off unless a code is passed |
| `TestMode(ttl=)` | required with a code | Positive, and at most `MAX_TEST_MODE_TTL` (24h) |
| `TestMode(policy=)` | `DEFAULT_POLICY` | Normalization applied to the universal code |
| `TestMode(logger=)` | `otpguard.testmode` logger | Where CRITICAL, ERROR and WARNING lines go |
| `require_no_test_mode(mode)` | — | Raises `TestModeInProduction` while a code is configured |

Environment variables, read by `TestMode.from_env()`:

| Variable | Meaning |
| --- | --- |
| `OTPGUARD_TEST_MODE_CODE` | The universal code; absent or blank leaves the mode off |
| `OTPGUARD_TEST_MODE_TTL` | Lifetime in seconds; mandatory when a code is set |
| `OTPGUARD_ENV` | Deployment name; `live`, `prod` or `production` refuses a code outright |

## Development

```bash
pip install -e .
python -m pytest
```

## License

MIT

Maintained by [Shipmind Labs](https://shipmindlabs.com).
