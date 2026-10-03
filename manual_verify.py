import re
import time
from datetime import datetime, timezone

from discord import app_commands

from cogs.common import guard, guard_assigned_roles
from services.rules import is_staff


def resolve_member(members, query):
    query = query.strip()
    match = re.fullmatch(r"<@!?(\d+)>|([0-9]+)", query)
    if match:
        uid = int(match.group(1) or match.group(2))
        found = [m for m in members if m.id == uid]
    else:
        name = query.removeprefix("@").casefold()
        found = [m for m in members if name in {m.name.casefold(), m.display_name.casefold(),
                                               (getattr(m, "global_name", None) or "").casefold()}]
    if not found:
        raise app_commands.CheckFailure("Участник не найден. Укажите точный username, упоминание или ID.")
    if len(found) != 1:
        raise app_commands.CheckFailure("Это имя совпадает у нескольких участников. Укажите упоминание или ID.")
    return found[0]


async def manually_verify(bot, interaction, member):
    """Caller holds member_lock. Recheck authority for every member of a bulk job."""
    if not is_staff(interaction.user, bot.cfg(member.guild.id)):
        raise app_commands.CheckFailure("У сотрудника больше нет доступа к команде.")
    if member.bot:
        raise app_commands.CheckFailure("Ботов верифицировать не нужно.")
    await guard(interaction, member, permission="manage_roles")
    guard_assigned_roles(interaction)
    gid, uid = member.guild.id, member.id
    state = await bot.db.state(gid, uid)
    current = member.timed_out_until.timestamp() if member.timed_out_until else 0
    # Only a timeout matching our recorded verification restriction belongs to this workflow.
    if (state and state["status"] != "verified" and current > time.time()
            and abs(current - state["timeout_until"]) <= 2):
        await guard(interaction, member, permission="moderate_members", timeout=True)
        until = state["moderation_until"]
        await member.timeout(datetime.fromtimestamp(until, timezone.utc) if until > time.time() else None,
                             reason=f"SecurityAgent: manual verification by {interaction.user.id}")
    await bot.db.ensure_user(gid, uid)
    await bot.db.mark_verified(gid, uid)
    await bot.audit(gid, uid, interaction.user.id, "manual_verified", "Staff approved without captcha")
    ok = await bot.roles.apply(member, verified=True)
    if not ok:
        await bot.audit(gid, uid, interaction.user.id, "manual_verify_role_repair_required")
    return ok
