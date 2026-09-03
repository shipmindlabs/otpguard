"""Generation, hashing and constant-time verification of one-time codes."""

from __future__ import annotations

import base64
import hmac
import math
import secrets
from dataclasses import dataclass
from hashlib import sha256

__all__ = [
    "ALGORITHM",
    "DEFAULT_POLICY",
    "DEFAULT_SEPARATORS",
    "DIGITS",
    "SALT_BYTES",
    "UPPERCASE_UNAMBIGUOUS",
    "CodePolicy",
    "HashedCode",
    "generate_code",
    "hash_code",
    "verify_code",
]

ALGORITHM = "hmac-sha256"
SALT_BYTES = 16

DIGITS = "0123456789"
#: Uppercase letters and digits without the visually ambiguous 0/O, 1/I, 5/S.
UPPERCASE_UNAMBIGUOUS = "ABCDEFGHJKLMNPQRTUVWXYZ2346789"
DEFAULT_SEPARATORS = " -\t\r\n"


@dataclass(frozen=True)
class CodePolicy:
    """Shape of a one-time code and how user input is normalized before matching."""

    length: int = 6
    alphabet: str = DIGITS
    case_sensitive: bool = False
    separators: str = DEFAULT_SEPARATORS

    def __post_init__(self) -> None:
        if self.length < 4:
            raise ValueError("length must be at least 4")
        if len(self.alphabet) < 2:
            raise ValueError("alphabet must contain at least two characters")
        comparable = self.alphabet if self.case_sensitive else self.alphabet.upper()
        if len(set(comparable)) != len(comparable):
            raise ValueError("alphabet must not contain duplicate characters")
        if set(self.alphabet) & set(self.separators):
            raise ValueError("separators must not overlap the alphabet")

    @property
    def entropy_bits(self) -> float:
        return self.length * math.log2(len(self.alphabet))

    def normalize(self, raw: str) -> str:
        """Drop separators and, for case-insensitive policies, fold the case."""
        stripped = "".join(ch for ch in raw if ch not in self.separators)
        return stripped if self.case_sensitive else stripped.upper()


DEFAULT_POLICY = CodePolicy()


@dataclass(frozen=True)
class HashedCode:
    """A salted digest of a code, safe to persist in place of the code itself."""

    algorithm: str
    salt: bytes
    digest: bytes

    def encode(self) -> str:
        return "$".join((self.algorithm, _b64(self.salt), _b64(self.digest)))

    @classmethod
    def decode(cls, encoded: str) -> HashedCode:
        parts = encoded.split("$")
        if len(parts) != 3:
            raise ValueError("malformed hashed code")
        algorithm, salt, digest = parts
        try:
            return cls(algorithm=algorithm, salt=_unb64(salt), digest=_unb64(digest))
        except (ValueError, base64.binascii.Error) as exc:  # type: ignore[attr-defined]
            raise ValueError("malformed hashed code") from exc

    def __str__(self) -> str:
        return self.encode()


def generate_code(policy: CodePolicy = DEFAULT_POLICY) -> str:
    """Draw a fresh code from the policy alphabet using the system CSPRNG."""
    return "".join(secrets.choice(policy.alphabet) for _ in range(policy.length))


def hash_code(
    code: str,
    policy: CodePolicy = DEFAULT_POLICY,
    *,
    pepper: bytes | str = b"",
    salt: bytes | None = None,
) -> HashedCode:
    """Hash a code for storage.

    A fresh salt is drawn unless one is supplied, so the same code never yields
    the same digest twice. The optional pepper lets an application bind digests
    to a secret kept outside the store.
    """
    if salt is None:
        salt = secrets.token_bytes(SALT_BYTES)
    elif not salt:
        raise ValueError("salt must not be empty")
    key = salt + _as_bytes(pepper)
    digest = hmac.new(key, policy.normalize(code).encode("utf-8"), sha256).digest()
    return HashedCode(algorithm=ALGORITHM, salt=salt, digest=digest)


def verify_code(
    candidate: str,
    hashed: HashedCode | str,
    policy: CodePolicy = DEFAULT_POLICY,
    *,
    pepper: bytes | str = b"",
) -> bool:
    """Check a candidate against a stored digest without leaking timing."""
    if isinstance(hashed, str):
        hashed = HashedCode.decode(hashed)
    if hashed.algorithm != ALGORITHM:
        return False
    computed = hash_code(candidate, policy, pepper=pepper, salt=hashed.salt)
    return hmac.compare_digest(computed.digest, hashed.digest)


def _as_bytes(value: bytes | str) -> bytes:
    return value.encode("utf-8") if isinstance(value, str) else value


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
