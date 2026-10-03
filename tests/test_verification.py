import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from cogs.verification import Verification, VerificationView
from services.moderation_service import ModerationService


async def test_new_join_timeout_dm_no_join_roles(fake_bot, member):
    verification = Verification(fake_bot)
    before = time.time()
    await verification.on_member_join(member)
    assert before + 7*86400 <= member.timed_out_until.timestamp() < before + 7*86400 + 5
    assert (await fake_bot.db.state(1, member.id))["status"] == "pending"
    member.send.assert_awaited_once()
    fake_bot.roles.apply.assert_awaited_once_with(member, verified=False)


async def test_complete_removes_timeout_then_marks_verified_and_roles(fake_bot, member):
    verification = Verification(fake_bot)
    await verification.on_member_join(member)
    await fake_bot.db.execute("UPDATE verification SET status='passed' WHERE guild_id=1 AND user_id=?", (member.id,))
    assert await verification.complete(member)
    assert member.timed_out_until is None
    assert (await fake_bot.db.state(1, member.id))["status"] == "verified"
    fake_bot.roles.apply.assert_awaited_with(member, verified=True)


async def test_verified_rejoin_skips_timeout_and_captcha(fake_bot, member):
    await fake_bot.db.ensure_user(1, member.id)
    await fake_bot.db.mark_verified(1, member.id)
    await Verification(fake_bot).on_member_join(member)
    member.send.assert_not_awaited()
    member.timeout.assert_not_awaited()
    fake_bot.roles.apply.assert_awaited_with(member, verified=True)


async def test_dm_disabled_posts_fallback_without_lifting_timeout(fake_bot, member):
    response = SimpleNamespace(status=403, reason="Forbidden")
    member.send.side_effect = discord.Forbidden(response, "DM closed")
    channel = SimpleNamespace(send=AsyncMock())
    member.guild.get_channel = lambda _: channel
    await Verification(fake_bot).on_member_join(member)
    channel.send.assert_awaited_once()
    assert member.timed_out_until is not None
    assert (await fake_bot.db.state(1, member.id))["status"] == "pending"


async def test_role_failure_keeps_verified_for_repair(fake_bot, member):
    verification = Verification(fake_bot)
    await verification.on_member_join(member)
    fake_bot.roles.apply.return_value = False
    await fake_bot.db.execute("UPDATE verification SET status='passed' WHERE guild_id=1 AND user_id=?", (member.id,))
    assert not await verification.complete(member)
    assert member.timed_out_until is None
    assert (await fake_bot.db.state(1, member.id))["status"] == "verified"


async def test_timeout_failure_retains_passed_for_retry(fake_bot, member):
    verification = Verification(fake_bot)
    await verification.on_member_join(member)
    await fake_bot.db.execute("UPDATE verification SET status='passed' WHERE guild_id=1 AND user_id=?", (member.id,))
    member.timeout.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "permissions")
    with pytest.raises(discord.Forbidden):
        await verification.complete(member)
    assert (await fake_bot.db.state(1, member.id))["status"] == "passed"


async def test_external_moderation_timeout_preserved(fake_bot, member):
    verification = Verification(fake_bot)
    await verification.on_member_join(member)
    external = datetime.fromtimestamp(time.time()+1200, timezone.utc)
    member.timed_out_until = external
    await fake_bot.db.execute("UPDATE verification SET status='passed' WHERE guild_id=1 AND user_id=?", (member.id,))
    await verification.complete(member)
    assert member.timed_out_until == external


async def test_bot_moderation_timeout_preserved(fake_bot, member):
    verification = Verification(fake_bot)
    await verification.on_member_join(member)
    await ModerationService(fake_bot).timeout(member, 10, "test")
    assert member.timed_out_until.timestamp() > time.time()+6*86400
    await fake_bot.db.execute("UPDATE verification SET status='passed' WHERE guild_id=1 AND user_id=?", (member.id,))
    await verification.complete(member)
    assert time.time()+590 <= member.timed_out_until.timestamp() <= time.time()+610


async def test_untimeout_keeps_verification_restriction(fake_bot, member):
    await Verification(fake_bot).on_member_join(member)
    await ModerationService(fake_bot).timeout(member, 0, "clear moderation")
    assert member.timed_out_until.timestamp() > time.time()+6*86400


async def test_view_persistent_and_per_guild(fake_bot):
    first, second = VerificationView(Verification(fake_bot), 1), VerificationView(Verification(fake_bot), 2)
    assert first.is_persistent()
    assert first.children[0].custom_id != second.children[0].custom_id


async def test_startup_guard_prevents_join_actions(fake_bot, member):
    fake_bot.operational = False
    await Verification(fake_bot).on_member_join(member)
    member.timeout.assert_not_awaited()
    assert await fake_bot.db.state(1, member.id) is None
