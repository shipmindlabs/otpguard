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

## Development

```bash
pip install -e .
python -m pytest
```

## License

MIT

Maintained by [Shipmind Labs](https://shipmindlabs.com).
