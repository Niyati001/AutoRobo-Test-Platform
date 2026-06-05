"""
Auth Service — Security utilities.

Provides:
- bcrypt password hashing and verification
- JWT creation and decoding (HS256)
- API key generation (raw + sha256 hash)
- Password strength validation
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple

from jose import JWTError, jwt
from passlib.context import CryptContext

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

JWT_SECRET: str = os.getenv("JWT_SECRET", "your-secret-key-change-in-production")
JWT_ALGORITHM: str = os.getenv("JWT_ALGORITHM", "HS256")
JWT_EXPIRE_MINUTES: int = int(os.getenv("JWT_EXPIRE_MINUTES", "60"))

# Passlib bcrypt context — auto-selects bcrypt rounds
_pwd_context = CryptContext(
    schemes=["bcrypt"],
    deprecated="auto",
    bcrypt__rounds=12,
)


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    """Hash a plain-text password using bcrypt. Returns the hashed string."""
    return _pwd_context.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    """Verify a plain-text password against a bcrypt hash."""
    try:
        return _pwd_context.verify(password, hashed)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Password strength validation
# ---------------------------------------------------------------------------

class PasswordValidationError(ValueError):
    pass


def validate_password_strength(password: str) -> None:
    """
    Raises PasswordValidationError if password fails strength requirements:
    - Minimum 8 characters
    - At least one uppercase letter
    - At least one lowercase letter
    - At least one digit
    """
    errors: list[str] = []
    if len(password) < 8:
        errors.append("at least 8 characters required")
    if not re.search(r"[A-Z]", password):
        errors.append("at least one uppercase letter required")
    if not re.search(r"[a-z]", password):
        errors.append("at least one lowercase letter required")
    if not re.search(r"\d", password):
        errors.append("at least one digit required")
    if errors:
        raise PasswordValidationError("; ".join(errors))


# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------

def create_access_token(data: Dict, expires_delta: Optional[timedelta] = None) -> str:
    """
    Create a signed JWT access token.

    Args:
        data: Payload dict. Must contain 'sub' (user UUID string), 'username', 'role'.
        expires_delta: Token lifetime. Defaults to JWT_EXPIRE_MINUTES.

    Returns:
        Signed JWT string.
    """
    to_encode = dict(data)
    expire = datetime.now(tz=timezone.utc) + (
        expires_delta if expires_delta is not None
        else timedelta(minutes=JWT_EXPIRE_MINUTES)
    )
    to_encode["exp"] = expire
    to_encode["iat"] = datetime.now(tz=timezone.utc)
    return jwt.encode(to_encode, JWT_SECRET, algorithm=JWT_ALGORITHM)


def create_refresh_token(user_id: str) -> str:
    """
    Create a signed JWT refresh token with a 7-day expiry.

    Args:
        user_id: User UUID string used as the 'sub' claim.

    Returns:
        Signed JWT string.
    """
    return create_access_token(
        {"sub": user_id, "type": "refresh"},
        expires_delta=timedelta(days=7),
    )


def decode_access_token(token: str) -> Dict:
    """
    Decode and validate a JWT access token.

    Args:
        token: JWT string.

    Returns:
        Decoded payload dict.

    Raises:
        JWTError: If the token is invalid, expired, or has wrong signature.
    """
    payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    return payload


def decode_refresh_token(token: str) -> str:
    """
    Decode a refresh token and return the user_id ('sub').

    Raises:
        JWTError: If invalid or expired.
        ValueError: If token type is not 'refresh'.
    """
    payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    if payload.get("type") != "refresh":
        raise ValueError("Token is not a refresh token")
    sub = payload.get("sub")
    if not sub:
        raise ValueError("Refresh token missing 'sub' claim")
    return sub


# ---------------------------------------------------------------------------
# API Key generation
# ---------------------------------------------------------------------------

def generate_api_key() -> Tuple[str, str]:
    """
    Generate a new API key pair.

    Returns:
        (raw_key, hashed_key) where:
        - raw_key: human-readable key of the form `arvp_{64 hex chars}`
        - hashed_key: SHA-256 hex digest of raw_key (stored in DB)
    """
    random_bytes = secrets.token_hex(32)  # 32 bytes → 64 hex chars
    raw_key = f"arvp_{random_bytes}"
    hashed_key = hash_api_key(raw_key)
    return raw_key, hashed_key


def hash_api_key(key: str) -> str:
    """Compute the SHA-256 hex digest of an API key."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()
