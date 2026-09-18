# otpguard

Passwordless one-time codes done safely: resend cooldown, attempt counting,
lockout and pluggable delivery channels.

## Status

Early development. The public API is not stable yet.

## Installation

```bash
pip install otpguard
```

## Usage

Codes are generated from the system CSPRNG, stored as a salted HMAC-SHA256
digest and compared in constant time. The clear-text code exists only long
enough to be delivered to the user.

```python
from otpguard import CodePolicy, generate_code, hash_code, verify_code

code = generate_code()                  # '482913'
stored = hash_code(code).encode()       # 'hmac-sha256$<salt>$<digest>'

verify_code("482913", stored)           # True
verify_code("000000", stored)           # False
```

Length and alphabet are configurable. The bundled `UPPERCASE_UNAMBIGUOUS`
alphabet drops characters that are easy to confuse when a code is read aloud:

```python
from otpguard import UPPERCASE_UNAMBIGUOUS, CodePolicy

policy = CodePolicy(length=8, alphabet=UPPERCASE_UNAMBIGUOUS)
policy.entropy_bits          # 39.2

code = generate_code(policy)            # 'K7H2MQ4B'
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
from datetime import datetime, timedelta, timezone
from otpguard import ResendCooldown, ResendTooSoon

cooldown = ResendCooldown(timedelta(seconds=60))

try:
    cooldown.check(last_sent_at)        # None on the first request
except ResendTooSoon as exc:
    exc.retry_after                     # timedelta(seconds=43)
    exc.retry_after_seconds             # 43.0, ready for a Retry-After header
else:
    send(generate_code())
    last_sent_at = datetime.now(timezone.utc)
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
record = AttemptRecord.decode(stored_record) if stored_record else None

try:
    lockout.check(record)
except LockedOut as exc:
    exc.retry_after_seconds             # 812.4
else:
    if verify_code(candidate, stored):
        record = lockout.register_success()
    else:
        record = lockout.register_failure(record)
        lockout.remaining_attempts(record)   # 2

stored_record = record.encode()         # '{"failures":3}'
```

The encoded record is ASCII JSON and fits in a single column. Once the lock
expires the tally starts over, and attempts made while the identifier is locked
leave the record untouched, so hammering the endpoint cannot stretch the wait.
Methods accept either an `AttemptRecord` or its encoded form.

## Development

```bash
pip install -e .
python -m pytest
```

## License

MIT

Maintained by [Shipmind Labs](https://shipmindlabs.com).
