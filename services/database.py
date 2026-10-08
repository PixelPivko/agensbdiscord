import asyncio
import hashlib
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

from services.rules import MAX_XP, captcha, level_for_xp, normalize

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY);
INSERT OR IGNORE INTO schema_version VALUES(1);
CREATE TABLE IF NOT EXISTS guild_state(guild_id INTEGER PRIMARY KEY,scanned_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS users(
 guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, verified INTEGER NOT NULL DEFAULT 0,
 joined_at REAL NOT NULL, PRIMARY KEY(guild_id,user_id));
CREATE TABLE IF NOT EXISTS verification(
 guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending'
 CHECK(status IN ('pending','passed','verified')), timeout_until REAL NOT NULL DEFAULT 0,
 moderation_until REAL NOT NULL DEFAULT 0, last_sent REAL NOT NULL DEFAULT 0,
 PRIMARY KEY(guild_id,user_id));
CREATE TABLE IF NOT EXISTS captcha_sessions(
 guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, nonce TEXT NOT NULL,
 question TEXT NOT NULL, expected_answer TEXT NOT NULL, created_at REAL NOT NULL,
 attempts INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(guild_id,user_id));
CREATE TABLE IF NOT EXISTS warnings(
 id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
 moderator_id INTEGER NOT NULL, reason TEXT NOT NULL, source TEXT NOT NULL,
 created_at REAL NOT NULL, active INTEGER NOT NULL DEFAULT 1, event_id INTEGER);
CREATE UNIQUE INDEX IF NOT EXISTS warning_event ON warnings(guild_id,event_id)
 WHERE event_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS warning_user ON warnings(guild_id,user_id,active);
CREATE TABLE IF NOT EXISTS xp(
 guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, xp INTEGER NOT NULL DEFAULT 0,
 last_xp_at REAL NOT NULL DEFAULT 0, PRIMARY KEY(guild_id,user_id));
CREATE TABLE IF NOT EXISTS xp_events(
 guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
 fingerprint TEXT NOT NULL, created_at REAL NOT NULL, amount INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(guild_id,message_id));
CREATE INDEX IF NOT EXISTS xp_events_user ON xp_events(guild_id,user_id,created_at);
CREATE TABLE IF NOT EXISTS deleted_messages(
 guild_id INTEGER NOT NULL, message_id INTEGER NOT NULL, created_at REAL NOT NULL,
 PRIMARY KEY(guild_id,message_id));
CREATE TABLE IF NOT EXISTS moderation_actions(
 id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
 moderator_id INTEGER NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL,
 created_at REAL NOT NULL, delivered INTEGER NOT NULL DEFAULT 0);
"""


class Database:
    """One serialized connection; multi-statement operations are atomic, including cancellation."""

    def __init__(self, path: str):
        self.path = path
        self.lock = asyncio.Lock()
        self.connection = None

    async def open(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = await aiosqlite.connect(self.path)
        self.connection.row_factory = aiosqlite.Row
        await self.connection.execute("PRAGMA journal_mode=WAL")
        await self.connection.execute("PRAGMA busy_timeout=5000")
        await self.connection.executescript(SCHEMA)
        await self.connection.commit()

    async def close(self):
        if self.connection:
            await self.connection.close()
            self.connection = None

    @asynccontextmanager
    async def transaction(self):
        async with self.lock:
            try:
                yield self.connection
                await self.connection.commit()
            except BaseException:
                await self.connection.rollback()
                raise

    async def rows(self, sql, params=()):
        async with self.transaction() as db:
            async with db.execute(sql, params) as cursor:
                return [dict(row) for row in await cursor.fetchall()]

    async def execute(self, sql, params=()):
        async with self.transaction() as db:
            cursor = await db.execute(sql, params)
            return cursor.rowcount

    async def ensure_user(self, guild_id, user_id, *, enroll=True):
        async with self.transaction() as db:
            await db.execute("INSERT OR IGNORE INTO users VALUES(?,?,0,?)", (guild_id, user_id, time.time()))
            if enroll:
                await db.execute("INSERT OR IGNORE INTO verification(guild_id,user_id) VALUES(?,?)",
                                 (guild_id, user_id))
            await db.execute("INSERT OR IGNORE INTO xp(guild_id,user_id) VALUES(?,?)", (guild_id, user_id))

    async def state(self, guild_id, user_id):
        rows = await self.rows("SELECT * FROM verification WHERE guild_id=? AND user_id=?", (guild_id, user_id))
        return rows[0] if rows else None

    async def reset(self, guild_id, user_id):
        await self.ensure_user(guild_id, user_id)
        async with self.transaction() as db:
            await db.execute("UPDATE verification SET status='pending',last_sent=0 WHERE guild_id=? AND user_id=?",
                             (guild_id, user_id))
            await db.execute("UPDATE users SET verified=0 WHERE guild_id=? AND user_id=?", (guild_id, user_id))
            await db.execute("DELETE FROM captcha_sessions WHERE guild_id=? AND user_id=?", (guild_id, user_id))

    async def mark_verified(self, guild_id, user_id):
        async with self.transaction() as db:
            await db.execute("UPDATE verification SET status='verified',timeout_until=0 WHERE guild_id=? AND user_id=?",
                             (guild_id, user_id))
            await db.execute("UPDATE users SET verified=1 WHERE guild_id=? AND user_id=?", (guild_id, user_id))
            await db.execute("DELETE FROM captcha_sessions WHERE guild_id=? AND user_id=?", (guild_id, user_id))

    async def challenge(self, guild_id, user_id, ttl, max_attempts, now=None):
        now = time.time() if now is None else now
        async with self.transaction() as db:
            async with db.execute("SELECT * FROM captcha_sessions WHERE guild_id=? AND user_id=?",
                                  (guild_id, user_id)) as cursor:
                row = await cursor.fetchone()
            if row and now - row["created_at"] < ttl and row["attempts"] < max_attempts:
                return dict(row)
            if row and now - row["created_at"] < ttl:
                raise ValueError("Попытки на этот пример закончились. Дождись окончания его срока и жми кнопку снова — выдадим свежий.")
            question, answer = captcha()
            nonce = secrets.token_hex(16)
            await db.execute("INSERT OR REPLACE INTO captcha_sessions VALUES(?,?,?,?,?,?,0)",
                             (guild_id, user_id, nonce, question, answer, now))
            return {"nonce": nonce, "question": question, "created_at": now, "attempts": 0}

    async def answer(self, guild_id, user_id, nonce, answer, ttl, max_attempts, now=None):
        now = time.time() if now is None else now
        async with self.transaction() as db:
            async with db.execute("SELECT * FROM captcha_sessions WHERE guild_id=? AND user_id=?",
                                  (guild_id, user_id)) as cursor:
                row = await cursor.fetchone()
            if not row or row["nonce"] != nonce or now - row["created_at"] >= ttl:
                return "expired", 0
            if row["attempts"] >= max_attempts:
                return "locked", 0
            attempts = row["attempts"] + 1
            await db.execute("UPDATE captcha_sessions SET attempts=? WHERE guild_id=? AND user_id=?",
                             (attempts, guild_id, user_id))
            if secrets.compare_digest(answer.strip().encode(), row["expected_answer"].encode()):
                await db.execute("UPDATE verification SET status='passed' WHERE guild_id=? AND user_id=?",
                                 (guild_id, user_id))
                await db.execute("DELETE FROM captcha_sessions WHERE guild_id=? AND user_id=?", (guild_id, user_id))
                return "passed", max_attempts - attempts
            return "incorrect", max_attempts - attempts

    async def add_warning(self, guild_id, user_id, moderator_id, reason, source, event_id=None):
        async with self.transaction() as db:
            cursor = await db.execute(
                "INSERT OR IGNORE INTO warnings(guild_id,user_id,moderator_id,reason,source,created_at,event_id) "
                "VALUES(?,?,?,?,?,?,?)", (guild_id, user_id, moderator_id, reason, source, time.time(), event_id))
            if cursor.rowcount == 0:
                return None, 0
            warning_id = cursor.lastrowid
            async with db.execute("SELECT COUNT(*) FROM warnings WHERE guild_id=? AND user_id=? AND active=1",
                                  (guild_id, user_id)) as cursor:
                count = (await cursor.fetchone())[0]
            return warning_id, count

    async def xp_value(self, guild_id, user_id):
        rows = await self.rows("SELECT xp FROM xp WHERE guild_id=? AND user_id=?", (guild_id, user_id))
        return rows[0]["xp"] if rows else 0

    async def change_xp(self, guild_id, user_id, amount, mode):
        await self.ensure_user(guild_id, user_id, enroll=False)
        async with self.transaction() as db:
            async with db.execute("SELECT xp FROM xp WHERE guild_id=? AND user_id=?", (guild_id, user_id)) as cursor:
                old = (await cursor.fetchone())[0]
            new = max(0, min(MAX_XP, amount if mode == "set" else old + amount))
            await db.execute("UPDATE xp SET xp=? WHERE guild_id=? AND user_id=?", (new, guild_id, user_id))
            # A staff override supersedes pending automatic credit revocations.
            await db.execute("UPDATE xp_events SET amount=0 WHERE guild_id=? AND user_id=?", (guild_id, user_id))
            return old, new

    async def award_xp(self, guild_id, user_id, message_id, content, cfg, now=None):
        now = time.time() if now is None else now
        fingerprint = hashlib.sha256(normalize(content).encode()).hexdigest()
        async with self.transaction() as db:
            async with db.execute("SELECT 1 FROM deleted_messages WHERE guild_id=? AND message_id=?",
                                  (guild_id, message_id)) as cursor:
                if await cursor.fetchone():
                    return 0, 0, 0
            async with db.execute("SELECT xp.xp,xp.last_xp_at,users.verified FROM xp JOIN users "
                                  "USING(guild_id,user_id) WHERE guild_id=? AND user_id=?",
                                  (guild_id, user_id)) as cursor:
                row = await cursor.fetchone()
            if not row or not row["verified"]:
                return 0, 0, 0
            old = row["xp"]
            async with db.execute("SELECT COUNT(*) AS n,MAX(CASE WHEN fingerprint=? AND created_at>? "
                                  "THEN 1 ELSE 0 END) AS duplicate FROM xp_events "
                                  "WHERE guild_id=? AND user_id=? AND created_at>?",
                                  (fingerprint, now - cfg["duplicate_window_seconds"], guild_id, user_id,
                                   now - max(cfg["duplicate_window_seconds"], cfg["spam_window_seconds"]))) as cursor:
                history = await cursor.fetchone()
            async with db.execute("SELECT COUNT(*) FROM xp_events WHERE guild_id=? AND user_id=? AND created_at>?",
                                  (guild_id, user_id, now - cfg["spam_window_seconds"])) as cursor:
                spam = (await cursor.fetchone())[0] >= cfg["spam_max_messages"]
            cursor = await db.execute("INSERT OR IGNORE INTO xp_events VALUES(?,?,?,?,?,0)",
                                      (guild_id, user_id, message_id, fingerprint, now))
            if cursor.rowcount == 0 or history["duplicate"] or spam or now-row["last_xp_at"] < cfg["message_cooldown_seconds"]:
                return old, old, 0
            amount = min(MAX_XP-old, secrets.randbelow(cfg["xp_max"]-cfg["xp_min"]+1)+cfg["xp_min"])
            await db.execute("UPDATE xp SET xp=?,last_xp_at=? WHERE guild_id=? AND user_id=?",
                             (old+amount, now, guild_id, user_id))
            await db.execute("UPDATE xp_events SET amount=? WHERE guild_id=? AND message_id=?",
                             (amount, guild_id, message_id))
            return old, old + amount, amount

    async def revoke_xp(self, guild_id, message_id):
        async with self.transaction() as db:
            # A delete event may arrive after fetch_message but before the XP transaction.
            await db.execute("INSERT OR IGNORE INTO deleted_messages VALUES(?,?,?)", (guild_id, message_id, time.time()))
            async with db.execute("SELECT user_id,amount FROM xp_events WHERE guild_id=? AND message_id=?",
                                  (guild_id, message_id)) as cursor:
                row = await cursor.fetchone()
            if not row or not row["amount"]:
                return None
            await db.execute("UPDATE xp SET xp=MAX(0,xp-?) WHERE guild_id=? AND user_id=?",
                             (row["amount"], guild_id, row["user_id"]))
            await db.execute("UPDATE xp_events SET amount=0 WHERE guild_id=? AND message_id=?", (guild_id, message_id))
            return row["user_id"]

    async def audit(self, guild_id, user_id, actor_id, action, detail=""):
        await self.execute("INSERT INTO moderation_actions(guild_id,user_id,moderator_id,action,detail,created_at) "
                           "VALUES(?,?,?,?,?,?)", (guild_id, user_id, actor_id, action, detail[:1000], time.time()))

    async def leaderboard(self, guild_id):
        return await self.rows("SELECT xp.user_id,xp.xp FROM xp JOIN users USING(guild_id,user_id) "
                               "WHERE guild_id=? AND users.verified=1 ORDER BY xp DESC,xp.user_id LIMIT 10", (guild_id,))

    async def level(self, guild_id, user_id):
        return level_for_xp(await self.xp_value(guild_id, user_id))
