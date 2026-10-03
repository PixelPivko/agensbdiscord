from datetime import datetime, timezone
import time

import discord

from services.rules import hierarchy_error


class ModerationService:
    def __init__(self, bot):
        self.bot = bot

    async def timeout(self, member, minutes, reason):
        gid, uid = member.guild.id, member.id
        state = await self.bot.db.state(gid, uid)
        until = time.time() + minutes * 60 if minutes else 0
        # Never shorten an active verification timeout when applying/removing a moderation timeout.
        effective = max(until, state["timeout_until"] if state and state["status"] == "pending" else 0)
        await member.timeout(datetime.fromtimestamp(effective, timezone.utc) if effective else None, reason=reason)
        if state:
            await self.bot.db.execute("UPDATE verification SET moderation_until=? WHERE guild_id=? AND user_id=?",
                                      (until, gid, uid))

    async def warn(self, member, actor, reason, source="manual", event_id=None):
        gid = member.guild.id
        warning_id, count = await self.bot.db.add_warning(gid, member.id, actor.id, reason, source, event_id)
        if warning_id is None:
            return None, 0
        await self.bot.audit(gid, member.id, actor.id, "warn", f"#{warning_id}; active={count}; {reason}")
        action = self.bot.cfg(gid)["moderation"]["warn_actions"].get(count, {})
        if action.get("notify_staff"):
            await self.bot.audit(gid, member.id, actor.id, "staff_attention", f"Active warnings: {count}")
        if action.get("ban") or action.get("timeout_minutes"):
            error = hierarchy_error(actor, member, member.guild.me, timeout=not action.get("ban"))
            permission = "ban_members" if action.get("ban") else "moderate_members"
            if error or not getattr(member.guild.me.guild_permissions, permission):
                await self.bot.audit(gid, member.id, actor.id, "escalation_blocked", error or permission)
            else:
                try:
                    if action.get("ban"):
                        await member.ban(reason=f"Warn threshold {count}", delete_message_seconds=0)
                        await self.bot.audit(gid, member.id, actor.id, "ban", f"Warn threshold {count}")
                    else:
                        await self.timeout(member, action["timeout_minutes"], f"Warn threshold {count}")
                        await self.bot.audit(gid, member.id, actor.id, "timeout", f"Warn threshold {count}")
                except discord.HTTPException as exc:
                    await self.bot.audit(gid, member.id, actor.id, "escalation_error", type(exc).__name__)
        return warning_id, count
