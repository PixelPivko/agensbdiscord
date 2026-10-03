import asyncio
from collections import defaultdict
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.config import load_config
from services.database import Database


@pytest.fixture
def config():
    cfg = load_config(offline=True)
    cfg["guilds"][1] = cfg["guilds"].pop(0)
    return cfg


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.sqlite"))
    await database.open()
    yield database
    await database.close()


@pytest.fixture
def fake_bot(config, db):
    locks = defaultdict(asyncio.Lock)
    bot = SimpleNamespace(db=db, config=config, operational=True)
    bot.cfg = lambda gid: config["guilds"].get(gid)
    bot.member_lock = lambda gid, uid: locks[gid, uid]
    bot.audit = AsyncMock(side_effect=db.audit)
    bot.roles = SimpleNamespace(apply=AsyncMock(return_value=True))
    return bot


@pytest.fixture
def member():
    bot_member = SimpleNamespace(id=99, top_role=100, guild_permissions=SimpleNamespace(moderate_members=True,
                                                                                     ban_members=True))
    guild = SimpleNamespace(id=1, owner_id=1, me=bot_member)
    member = SimpleNamespace(id=10, guild=guild, bot=False, roles=[], top_role=2,
                             guild_permissions=SimpleNamespace(administrator=False),
                             timed_out_until=None, joined_at=datetime.now(timezone.utc),
                             send=AsyncMock(), add_roles=AsyncMock(), remove_roles=AsyncMock())
    async def timeout(until, **kwargs):
        member.timed_out_until = until
    member.timeout = AsyncMock(side_effect=timeout)
    member.ban = AsyncMock()
    guild.fetch_member = AsyncMock(return_value=member)
    guild.get_member = lambda uid: member if uid == member.id else None
    guild.get_channel = lambda cid: None
    return member
