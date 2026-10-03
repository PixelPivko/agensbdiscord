import discord
from discord import app_commands

from services.rules import hierarchy_error, is_staff


def embed(description: str, *, color=0x5865F2, title="АГЕНТ СЛУЖБЫ БЕЗОПАСНОСТИ"):
    result = discord.Embed(title=title, description=description[:4000], color=color)
    result.set_footer(text="ТОРТИК PROJECT")
    return result


async def reply(interaction, text, *, color=0x5865F2):
    kwargs = dict(embed=embed(text, color=color), ephemeral=True,
                  allowed_mentions=discord.AllowedMentions.none())
    if interaction.response.is_done():
        await interaction.followup.send(**kwargs)
    else:
        await interaction.response.send_message(**kwargs)


def staff_check():
    async def predicate(interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            raise app_commands.CheckFailure("Команда доступна только на сервере.")
        cfg = interaction.client.cfg(interaction.guild_id)
        if not cfg or not is_staff(interaction.user, cfg):
            raise app_commands.CheckFailure("Команда доступна только сотрудникам сервера.")
        return True
    return app_commands.check(predicate)


async def guard(interaction, target=None, *, permission=None, timeout=False):  # noqa: ASYNC109
    if permission and not getattr(interaction.guild.me.guild_permissions, permission):
        await interaction.client.audit(interaction.guild_id, target.id if target else 0,
                                       interaction.user.id, "bot_permission_error", permission)
        raise app_commands.CheckFailure(f"Боту необходимо право {permission}.")
    if target:
        error = hierarchy_error(interaction.user, target, interaction.guild.me, timeout=timeout)
        if error:
            raise app_commands.CheckFailure(error)


def guard_assigned_roles(interaction):
    if interaction.user.id == interaction.guild.owner_id:
        return
    cfg = interaction.client.cfg(interaction.guild_id)
    for rid in cfg["join_roles"] + [r for b in cfg["levels"]["brackets"] for r in b["roles"]]:
        role = interaction.guild.get_role(rid)
        if role and role >= interaction.user.top_role:
            raise app_commands.CheckFailure("Выдаваемые роли должны быть ниже вашей высшей роли.")


def reason_text(reason: str):
    if not reason.strip() or len(reason) > 400:
        raise app_commands.CheckFailure("Причина должна содержать от 1 до 400 символов.")
    return reason.strip()
