"""Auth primitives — argon2id password hashing, the session secret key, and the
first-run admin seed. Vetted libraries only (argon2-cffi + itsdangerous via
Starlette's SessionMiddleware); no bespoke crypto. See
docs/multi-user-and-pwa-design.md §3.
"""

from __future__ import annotations

import logging
import os
import secrets
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from franklinwh_bridge.store.db import (
    count_users,
    create_user,
    get_app_config,
    set_app_config,
)

logger = logging.getLogger(__name__)

_ph = PasswordHasher()
_SECRET_KEY_CFG = "security_secret_key"


def hash_password(password: str) -> str:
    """argon2id hash of a plaintext password."""
    return _ph.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """True iff ``password`` matches ``password_hash``. Never raises."""
    try:
        _ph.verify(password_hash, password)
        return True
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def generate_password(nbytes: int = 12) -> str:
    """A url-safe random password."""
    return secrets.token_urlsafe(nbytes)


async def get_or_create_secret_key(db: Any) -> str:
    """Session-signing secret: ``SECURITY_SECRET_KEY`` env, else a generated key
    persisted in app_config (stable across restarts)."""
    env = os.environ.get("SECURITY_SECRET_KEY")
    if env:
        return env
    existing = await get_app_config(db, _SECRET_KEY_CFG, None)
    if existing:
        return existing
    key = secrets.token_hex(32)
    await set_app_config(db, _SECRET_KEY_CFG, key)
    return key


async def seed_admin(db: Any) -> None:
    """On first run (no users) create an admin — from ``ADMIN_USERNAME`` /
    ``ADMIN_PASSWORD`` env, else a generated password **logged once**. This
    guarantees there's always a way in (no lockout)."""
    if await count_users(db) > 0:
        return
    username = os.environ.get("ADMIN_USERNAME", "admin")
    password = os.environ.get("ADMIN_PASSWORD")
    generated = password is None
    if generated:
        password = generate_password()
    await create_user(db, username, hash_password(password), role="admin", enabled=True)
    if generated:
        logger.warning(
            "Seeded admin user %r with a GENERATED password: %s  (set ADMIN_PASSWORD "
            "to choose your own; change it after first login)",
            username, password,
        )
    else:
        logger.info("Seeded admin user %r from ADMIN_USERNAME/ADMIN_PASSWORD env", username)
