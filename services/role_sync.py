import asyncio
import time

import discord

from services.rules import role_ids_for_level


class RoleSync:
    def __init__(self, bot):
        self.bot = bot
        self.request_lock = asyncio.Lock()
        self.next_request_at = 0.0
        self.request_interval = 0.5

    async def pace(self):
        # Shared across all role jobs in this bot, not just one bulk invocation.
        async with self.request_lock:
            await asyncio.sleep(max(0, self.next_request_at - time.monotonic()))
            self.next_request_at = time.monotonic() + self.request_interval

    async def apply(self, member, *, verified: bool):
        cfg = self.bot.cfg(member.guild.id)
        brackets = cfg["levels"]["brackets"]
        level_roles = {r for b in brackets for r in b["roles"]}
        owned = {r.id for r in member.roles}
        desired = (set(cfg["join_roles"]) | role_ids_for_level(
            await self.bot.db.level(member.guild.id, member.id), brackets)) if verified else set()
        managed = set(cfg["join_roles"]) | level_roles
        # Remove obsolete roles before adding the new pair; never edit unrelated roles.
        success = True
        for adding, ids in ((False, (owned & managed) - desired), (True, desired - owned)):
            for role_id in sorted(ids):
                role = member.guild.get_role(role_id)
                if (role is None or role.managed or role.is_default()
                        or role >= member.guild.me.top_role or not member.guild.me.guild_permissions.manage_roles
                        or role.permissions.administrator or role.permissions.manage_roles):
                    success = False
                    await self.bot.audit(member.guild.id, member.id, 0, "level_role_error",
                                         f"Role {role_id}: missing, unsafe, or not assignable")
                    continue
                try:
                    await self.pace()
                    if adding:
                        await member.add_roles(role, reason="SecurityAgent: verified role synchronization")
                    else:
                        await member.remove_roles(role, reason="SecurityAgent: role synchronization")
                except discord.HTTPException as exc:
                    success = False
                    await self.bot.audit(member.guild.id, member.id, 0, "level_role_error",
                                         f"Role {role_id}: {type(exc).__name__}")
            if not success and not adding:
                # Do not add a second pair if an old pair could not be removed.
                return False
        return success
