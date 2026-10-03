from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from discord import app_commands

from bot import SecurityBot
from cogs.automod import Automod
from cogs.common import guard, guard_assigned_roles, staff_check
from services.moderation_service import ModerationService
from services.role_sync import RoleSync
from services.rules import xp_for_level


@dataclass(order=True)
class Role:
    position: int
    id: int = field(compare=False)
    managed: bool = field(default=False, compare=False)
    permissions: object = field(default_factory=lambda: SimpleNamespace(administrator=False, manage_roles=False), compare=False)

    def is_default(self):
        return False


@pytest.mark.parametrize("old,new", [(4, 5), (9, 10), (14, 15), (19, 20), (20, 4), (999, 1000)])
async def test_real_role_service_transitions(fake_bot, member, old, new):
    cfg = fake_bot.cfg(1)
    ids = cfg["join_roles"] + [r for b in cfg["levels"]["brackets"] for r in b["roles"]]
    roles = {rid: Role(i+1, rid) for i, rid in enumerate(ids)}
    member.guild.get_role = roles.get
    member.guild.me.top_role = Role(100, 99)
    member.guild.me.guild_permissions.manage_roles = True
    old_pair = next(b["roles"] for b in cfg["levels"]["brackets"] if b["min"] <= old <= b["max"])
    new_pair = next(b["roles"] for b in cfg["levels"]["brackets"] if b["min"] <= new <= b["max"])
    unrelated = Role(1, 777)
    member.roles = [roles[r] for r in old_pair+cfg["join_roles"]]+[unrelated]
    sequence = []
    async def remove(role, **kwargs):
        sequence.append("remove")
        member.roles.remove(role)
    async def add(role, **kwargs):
        sequence.append("add")
        member.roles.append(role)
    member.remove_roles.side_effect, member.add_roles.side_effect = remove, add
    await fake_bot.db.change_xp(1, member.id, xp_for_level(new), "set")
    assert await RoleSync(fake_bot).apply(member, verified=True)
    assert {r.id for r in member.roles} == set(cfg["join_roles"] + new_pair + [777])
    assert sequence == sorted(sequence, reverse=True)  # removals before additions


async def test_bad_role_is_logged_and_does_not_crash(fake_bot, member):
    member.guild.get_role = lambda _: None
    member.guild.me.guild_permissions.manage_roles = True
    assert not await RoleSync(fake_bot).apply(member, verified=True)
    rows = await fake_bot.db.rows("SELECT action FROM moderation_actions WHERE guild_id=1")
    assert len(rows) == 9 and all(r["action"] == "level_role_error" for r in rows)


async def test_staff_check_rejects_regular_member(fake_bot):
    actor = Mock(spec=discord.Member)
    actor.id = 20
    actor.roles = []
    actor.guild.owner_id = 1
    actor.guild_permissions.administrator = False
    interaction = SimpleNamespace(guild=actor.guild, guild_id=1, user=actor, client=fake_bot)
    async def callback(interaction):
        pass
    command = app_commands.Command(name="sample", description="sample", callback=callback)
    command = staff_check()(command)
    with pytest.raises(app_commands.CheckFailure):
        await command.checks[0](interaction)
    actor.roles = [SimpleNamespace(id=fake_bot.cfg(1)["staff_role_id"])]
    assert await command.checks[0](interaction)


async def test_bot_permission_guard(fake_bot, member):
    member.guild.me.guild_permissions.ban_members = False
    actor = SimpleNamespace(id=20, top_role=50)
    interaction = SimpleNamespace(guild=member.guild, guild_id=1, user=actor, client=fake_bot)
    with pytest.raises(app_commands.CheckFailure):
        await guard(interaction, member, permission="ban_members")
    assert (await fake_bot.db.rows("SELECT action FROM moderation_actions"))[0]["action"] == "bot_permission_error"


def test_staff_cannot_assign_higher_role(fake_bot, member):
    member.guild.get_role = lambda _: Role(60, 500)
    actor = SimpleNamespace(id=20, top_role=Role(50, 501))
    interaction = SimpleNamespace(guild=member.guild, guild_id=1, user=actor, client=fake_bot)
    with pytest.raises(app_commands.CheckFailure):
        guard_assigned_roles(interaction)


async def test_configurable_warn_escalation(fake_bot, member):
    service = ModerationService(fake_bot)
    actor = SimpleNamespace(id=20, top_role=50)
    for _ in range(3):
        result = await service.warn(member, actor, "test")
    assert result[1] == 3
    member.timeout.assert_awaited_once()
    assert await fake_bot.db.state(1, member.id) is None  # moderation must not enroll an incumbent
    assert any(r["action"] == "timeout" for r in await fake_bot.db.rows("SELECT action FROM moderation_actions"))


async def test_escalation_hierarchy_blocks_punishment(fake_bot, member):
    service = ModerationService(fake_bot)
    actor = SimpleNamespace(id=20, top_role=1)
    for _ in range(3):
        await service.warn(member, actor, "test")
    member.timeout.assert_not_awaited()
    assert any(r["action"] == "escalation_blocked" for r in await fake_bot.db.rows("SELECT action FROM moderation_actions"))


async def test_profanity_delete_warn_audit_and_normal_message(fake_bot, member):
    fake_bot.moderation = ModerationService(fake_bot)
    message = SimpleNamespace(guild=member.guild, author=member, content="блять", id=123,
                              channel=SimpleNamespace(id=100), delete=AsyncMock())
    automod = Automod(fake_bot)
    assert await automod.moderate(message)
    message.delete.assert_awaited_once()
    assert len(await fake_bot.db.rows("SELECT * FROM warnings")) == 1
    assert any(r["action"] == "profanity_deleted" for r in await fake_bot.db.rows("SELECT action FROM moderation_actions"))
    assert await automod.moderate(message)  # duplicate event does not duplicate warning
    assert len(await fake_bot.db.rows("SELECT * FROM warnings")) == 1
    message.content = "Обычное сообщение"
    assert not await automod.moderate(message)


async def test_commands_serialize_and_privileged_commands_have_checks(config, tmp_path):
    config["database"] = str(tmp_path/"commands.db")
    async with SecurityBot(config) as bot:
        await bot.setup_local()
        expected = {"warn", "warnings", "unwarn", "clearwarns", "timeout", "untimeout", "kick", "ban",
                    "unban", "purge", "userinfo", "modlog", "verification", "level", "leaderboard", "xp", "levels"}
        assert {c.name for c in bot.tree.get_commands()} == expected
        for cmd in bot.tree.walk_commands():
            if not isinstance(cmd, app_commands.Group) and cmd.name not in {"level", "leaderboard"}:
                assert cmd.checks, cmd.qualified_name
        for cmd in bot.tree.get_commands():
            assert cmd.to_dict(bot.tree)["dm_permission"] is False
        assert bot.persistent_views


async def test_preflight_loads_no_mutating_listeners(config, tmp_path):
    config["database"] = str(tmp_path/"preflight.db")
    async with SecurityBot(config, preflight=True) as bot:
        await bot.setup_hook()
        assert not bot.cogs
        assert not bot.tree.get_commands()
        assert not bot.maintenance.is_running()
        assert not bot.operational
