"""Phase 1a: password hashing, user CRUD, secret key, admin seed."""

import pytest

from franklinwh_bridge.security import (
    get_or_create_secret_key,
    hash_password,
    seed_admin,
    verify_password,
)
from franklinwh_bridge.store.db import (
    count_users,
    create_user,
    delete_user,
    get_user,
    get_user_by_username,
    get_users,
    init_db,
    update_user,
)


@pytest.fixture
async def db(tmp_path):
    conn = await init_db(tmp_path / "auth.db")
    yield conn
    await conn.close()


def test_hash_and_verify():
    h = hash_password("s3cret!")
    assert h != "s3cret!" and h.startswith("$argon2")
    assert verify_password(h, "s3cret!") is True
    assert verify_password(h, "wrong") is False
    assert verify_password("not-a-hash", "x") is False  # never raises


async def test_user_crud_and_unique(db):
    u = await create_user(db, "alice", hash_password("pw"), role="admin")
    assert u["id"].startswith("user_") and u["role"] == "admin" and u["enabled"] is True
    assert (await get_user_by_username(db, "alice"))["id"] == u["id"]
    assert await count_users(db) == 1

    upd = await update_user(db, u["id"], role="user", enabled=False)
    assert upd["role"] == "user" and upd["enabled"] is False

    # password change via update
    await update_user(db, u["id"], password_hash=hash_password("new"))
    assert verify_password((await get_user(db, u["id"]))["password_hash"], "new")

    assert len(await get_users(db)) == 1
    assert await delete_user(db, u["id"]) is True
    assert await get_user(db, u["id"]) is None


async def test_secret_key_is_stable(db):
    k1 = await get_or_create_secret_key(db)
    k2 = await get_or_create_secret_key(db)
    assert k1 == k2 and len(k1) >= 32


async def test_seed_admin_generated_then_idempotent(db, monkeypatch, caplog):
    monkeypatch.delenv("ADMIN_USERNAME", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    import logging

    with caplog.at_level(logging.WARNING):
        await seed_admin(db)
    admin = await get_user_by_username(db, "admin")
    assert admin is not None and admin["role"] == "admin"
    assert "GENERATED password" in caplog.text  # logged once so the operator can log in

    # second call is a no-op (users already exist) — no second admin, no re-log
    await seed_admin(db)
    assert await count_users(db) == 1


async def test_seed_admin_from_env(db, monkeypatch):
    monkeypatch.setenv("ADMIN_USERNAME", "root")
    monkeypatch.setenv("ADMIN_PASSWORD", "hunter2")
    await seed_admin(db)
    admin = await get_user_by_username(db, "root")
    assert admin is not None
    assert verify_password(admin["password_hash"], "hunter2")
