import logging

import discord

REQUIRED = ("manage_roles", "manage_messages", "moderate_members", "kick_members", "ban_members",
            "view_channel", "send_messages", "embed_links", "read_message_history")


async def startup_check(bot, guild):
    cfg = bot.cfg(guild.id)
    me = guild.me
    missing = [name for name in REQUIRED if not getattr(me.guild_permissions, name)]
    staff = guild.get_role(cfg["staff_role_id"])
    report = {"guild": guild.id, "database": "OK", "staff_role": "OK" if staff else "MISSING",
              "missing_permissions": missing, "roles": {}, "channels": {}}
    errors = len(missing) + int(staff is None)
    for group, ids in (("join", cfg["join_roles"]),
                       ("level", [r for b in cfg["levels"]["brackets"] for r in b["roles"]])):
        ok = 0
        for rid in ids:
            role = guild.get_role(rid)
            valid = (role is not None and not role.managed and not role.is_default() and role < me.top_role
                     and not role.permissions.administrator and not role.permissions.manage_roles)
            ok += int(valid)
        report["roles"][group] = f"{ok}/{len(ids)}"
        errors += len(ids) - ok
    for name in ("verification_channel_id", "modlog_channel_id"):
        channel = guild.get_channel(cfg[name])
        valid = isinstance(channel, discord.TextChannel)
        if valid:
            p = channel.permissions_for(me)
            valid = p.view_channel and p.send_messages and p.embed_links and p.read_message_history
        report["channels"][name] = "OK" if valid else "MISSING/NO PERMISSION"
        errors += int(not valid)
    logging.getLogger("security.startup").log(logging.WARNING if errors else logging.INFO,
                                              "Startup check %s", report)
    return report, errors
