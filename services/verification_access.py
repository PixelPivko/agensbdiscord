import time
from datetime import datetime, timezone


async def release_legacy_timeout(bot, member, state):
    """Remove only a timeout owned by the old verification flow, preserving moderation."""
    if not state or not state["timeout_until"]:
        return
    current = member.timed_out_until.timestamp() if member.timed_out_until else 0
    if current > time.time() and abs(current - state["timeout_until"]) <= 2:
        until = state["moderation_until"]
        await member.timeout(datetime.fromtimestamp(until, timezone.utc) if until > time.time() else None,
                             reason="SecurityAgent: migrate verification timeout to pending role")
        await bot.audit(member.guild.id, member.id, 0, "verification_timeout_migrated")
    await bot.db.execute("UPDATE verification SET timeout_until=0 WHERE guild_id=? AND user_id=?",
                         (member.guild.id, member.id))
