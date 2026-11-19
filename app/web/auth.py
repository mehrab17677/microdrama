"""Password hashing and account helpers (argon2id)."""
from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

_hasher = PasswordHasher()  # argon2id by default
_DUMMY_HASH = _hasher.hash("not-a-real-password")  # verified when the email is unknown, to keep timing similar

MIN_PASSWORD_LENGTH = 8
SEAT_CAP = 5  # active users, admin included (guide section 4)


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerificationError, InvalidHashError):
        return False


def normalize_email(email: str) -> str:
    return email.strip().lower()


def validate_registration(email: str, display_name: str, password: str) -> list[str]:
    problems = []
    if "@" not in email or email.startswith("@") or email.endswith("@") or " " in email:
        problems.append("Enter a valid email address.")
    if not display_name.strip():
        problems.append("Enter a display name.")
    if len(password) < MIN_PASSWORD_LENGTH:
        problems.append(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    return problems
