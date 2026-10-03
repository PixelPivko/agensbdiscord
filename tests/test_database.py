import asyncio

import pytest

from services.database import Database
from services.rules import MAX_XP


async def test_captcha_attempts_expiry_and_cross_guild(db):
    for gid in (1, 2):
        await db.ensure_user(gid, 10)
    session = await db.challenge(1, 10, 600, 5, now=1000)
    stored = (await db.rows("SELECT * FROM captcha_sessions WHERE guild_id=1"))[0]
    assert (await db.answer(2, 10, session["nonce"], stored["expected_answer"], 600, 5, now=1001))[0] == "expired"
    for expected_remaining in range(4, -1, -1):
        assert await db.answer(1, 10, session["nonce"], "wrong", 600, 5, now=1001) == ("incorrect", expected_remaining)
    assert (await db.answer(1, 10, session["nonce"], stored["expected_answer"], 600, 5, now=1002))[0] == "locked"
    with pytest.raises(ValueError):
        await db.challenge(1, 10, 600, 5, now=1003)
    renewed = await db.challenge(1, 10, 600, 5, now=1600)
    assert renewed["nonce"] != session["nonce"]
    assert (await db.answer(1, 10, session["nonce"], "1", 600, 5, now=1601))[0] == "expired"


async def test_captcha_success_is_single_use_and_persistent(db):
    await db.ensure_user(1, 10)
    session = await db.challenge(1, 10, 600, 5, now=1000)
    stored = (await db.rows("SELECT expected_answer FROM captcha_sessions"))[0]
    result = await db.answer(1, 10, session["nonce"], stored["expected_answer"], 600, 5, now=1001)
    assert result[0] == "passed"
    assert (await db.state(1, 10))["status"] == "passed"
    assert (await db.answer(1, 10, session["nonce"], stored["expected_answer"], 600, 5, now=1002))[0] == "expired"
    await db.mark_verified(1, 10)
    await db.close()
    await db.open()
    assert (await db.state(1, 10))["status"] == "verified"
    await db.reset(1, 10)
    assert (await db.state(1, 10))["status"] == "pending"
    assert (await db.rows("SELECT verified FROM users"))[0]["verified"] == 0


async def test_captcha_expires_at_exact_boundary(db):
    await db.ensure_user(1, 10)
    session = await db.challenge(1, 10, 600, 5, now=1000)
    assert (await db.answer(1, 10, session["nonce"], "1", 600, 5, now=1600))[0] == "expired"


async def test_warning_dedup_and_guild_scope(db):
    first, count = await db.add_warning(1, 10, 99, "test", "antiprofanity", 500)
    assert first and count == 1
    assert await db.add_warning(1, 10, 99, "test", "antiprofanity", 500) == (None, 0)
    second, count = await db.add_warning(2, 10, 99, "test", "antiprofanity", 500)
    assert second != first and count == 1
    assert await db.execute("UPDATE warnings SET active=0 WHERE guild_id=? AND id=?", (2, first)) == 0


async def test_concurrent_xp_cooldown_and_duplicates(db, config):
    cfg = config["guilds"][1]["xp"]
    await db.ensure_user(1, 10)
    await db.mark_verified(1, 10)
    result = await asyncio.gather(*(db.award_xp(1, 10, i, f"normal message {i}", cfg, now=1000) for i in range(10)))
    assert sum(bool(r[2]) for r in result) == 1
    assert (await db.award_xp(1, 10, 20, "normal message 0", cfg, now=1061))[2] == 0
    assert (await db.award_xp(1, 10, 21, "new message", cfg, now=1061))[2] > 0
    assert (await db.award_xp(1, 10, 21, "new message", cfg, now=1122))[2] == 0


async def test_xp_unverified_no_award_and_multiguild(db, config):
    cfg = config["guilds"][1]["xp"]
    for gid in (1, 2):
        await db.ensure_user(gid, 10)
    await db.mark_verified(1, 10)
    assert (await db.award_xp(2, 10, 123, "hello world", cfg, now=1000))[2] == 0
    assert (await db.award_xp(1, 10, 123, "hello world", cfg, now=1000))[2] > 0
    assert await db.xp_value(2, 10) == 0


async def test_xp_delete_reversal_and_clamp(db, config):
    await db.ensure_user(1, 10)
    await db.mark_verified(1, 10)
    result = await db.award_xp(1, 10, 123, "hello world", config["guilds"][1]["xp"], now=1000)
    assert result[2] > 0
    assert await db.revoke_xp(2, 123) is None
    assert await db.revoke_xp(1, 123) == 10
    assert await db.revoke_xp(1, 123) is None
    assert await db.xp_value(1, 10) == 0
    assert (await db.change_xp(1, 10, MAX_XP+100, "set"))[1] == MAX_XP
    assert (await db.change_xp(1, 10, -MAX_XP-100, "add"))[1] == 0


async def test_cooldown_survives_reconnect(db, config):
    await db.ensure_user(1, 10)
    await db.mark_verified(1, 10)
    cfg = config["guilds"][1]["xp"]
    await db.award_xp(1, 10, 1, "first message", cfg, now=1000)
    await db.close()
    await db.open()
    assert (await db.award_xp(1, 10, 2, "second message", cfg, now=1020))[2] == 0


async def test_rollback_does_not_leak_partial_write(db):
    with pytest.raises(RuntimeError):
        async with db.transaction() as conn:
            await conn.execute("INSERT INTO users VALUES(1,10,0,0)")
            raise RuntimeError("rollback")
    assert not await db.rows("SELECT * FROM users")


async def test_xp_staff_change_does_not_enroll_existing_member(db):
    await db.change_xp(1, 10, 100, "set")
    assert await db.state(1, 10) is None


async def test_delete_before_award_race_is_blocked(db, config):
    await db.ensure_user(1, 10)
    await db.mark_verified(1, 10)
    await db.revoke_xp(1, 123)
    result = await db.award_xp(1, 10, 123, "hello world", config["guilds"][1]["xp"], now=1000)
    assert result[2] == 0
    assert await db.xp_value(1, 10) == 0


async def test_spam_window_is_independent_of_cooldown(db, config):
    await db.ensure_user(1, 10)
    await db.mark_verified(1, 10)
    cfg = {**config["guilds"][1]["xp"], "message_cooldown_seconds": 1}
    for i in range(5):
        assert (await db.award_xp(1, 10, i, f"message {i}", cfg, now=1000+i))[2] > 0
    assert (await db.award_xp(1, 10, 10, "sixth unique message", cfg, now=1005))[2] == 0


async def test_two_independent_databases(tmp_path):
    databases = [Database(str(tmp_path/f"{i}.db")) for i in range(2)]
    for database in databases:
        await database.open()
    try:
        await databases[0].ensure_user(1, 10)
        assert await databases[1].state(1, 10) is None
    finally:
        for database in databases:
            await database.close()
