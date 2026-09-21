"""Passwordless one-time codes with cooldown, attempt limits and lockout."""

from otpguard.codes import (
    ALGORITHM,
    DEFAULT_POLICY,
    DEFAULT_SEPARATORS,
    DIGITS,
    SALT_BYTES,
    UPPERCASE_UNAMBIGUOUS,
    CodePolicy,
    HashedCode,
    generate_code,
    hash_code,
    verify_code,
)
from otpguard.cooldown import DEFAULT_COOLDOWN, ResendCooldown, ResendTooSoon
from otpguard.lockout import (
    DEFAULT_LOCKOUT,
    DEFAULT_MAX_ATTEMPTS,
    AttemptRecord,
    LockedOut,
    LockoutPolicy,
)
from otpguard.storage import (
    DEFAULT_PREFIX,
    MemoryStorage,
    RedisStorage,
    Storage,
)

__all__ = [
    "ALGORITHM",
    "DEFAULT_COOLDOWN",
    "DEFAULT_LOCKOUT",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_POLICY",
    "DEFAULT_PREFIX",
    "DEFAULT_SEPARATORS",
    "DIGITS",
    "SALT_BYTES",
    "UPPERCASE_UNAMBIGUOUS",
    "AttemptRecord",
    "CodePolicy",
    "HashedCode",
    "LockedOut",
    "LockoutPolicy",
    "MemoryStorage",
    "RedisStorage",
    "ResendCooldown",
    "ResendTooSoon",
    "Storage",
    "__version__",
    "generate_code",
    "hash_code",
    "verify_code",
]

__version__ = "0.1.0"
