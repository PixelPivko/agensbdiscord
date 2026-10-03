import discord
from discord import app_commands
from discord.ext import commands

from cogs.common import guard, guard_assigned_roles, reply, staff_check
from services.rules import MAX_XP, level_for_xp, xp_for_level


@app_commands.guild_only()
class Levels(commands.Cog):
    xp = app_commands.Group(name="xp", description="Управление опытом", guild_only=True)
    levels = app_commands.Group(name="levels", description="Управление уровнями и ролями", guild_only=True)

    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="level", description="Посмотреть свой уровень или уровень участника")
    async def level(self, interaction: discord.Interaction, user: discord.Member | None = None):
        user = user or interaction.user
        xp = await self.bot.db.xp_value(interaction.guild_id, user.id)
        level = level_for_xp(xp)
        floor = xp_for_level(level)
        progress = (f"XP уровня: {xp-floor} / {xp_for_level(level+1)-floor}\n"
                    f"До следующего уровня: {xp_for_level(level+1)-xp} XP" if level < 1000 else "Максимальный уровень.")
        await reply(interaction, f"{discord.utils.escape_markdown(user.display_name)}\nУровень: **{level}**\n"
                    f"Всего XP: {xp}\n{progress}")

    @app_commands.command(description="Топ-10 участников по опыту")
    async def leaderboard(self, interaction: discord.Interaction):
        rows = await self.bot.db.leaderboard(interaction.guild_id)
        lines = []
        for index, row in enumerate(rows, 1):
            member = interaction.guild.get_member(row["user_id"])
            name = member.display_name if member else str(row["user_id"])
            lines.append(f"{index}. {discord.utils.escape_markdown(name)} — Level {level_for_xp(row['xp'])} ({row['xp']} XP)")
        await reply(interaction, "\n".join(lines) or "Рейтинг пока пуст.")

    async def adjust(self, interaction, user, amount, mode):
        await guard(interaction, user, permission="manage_roles")
        guard_assigned_roles(interaction)
        await interaction.response.defer(ephemeral=True)
        async with self.bot.member_lock(interaction.guild_id, user.id):
            old, new = await self.bot.db.change_xp(interaction.guild_id, user.id, amount, mode)
            state = await self.bot.db.state(interaction.guild_id, user.id)
            if state and state["status"] == "verified":
                await self.bot.roles.apply(user, verified=True)
            await self.bot.audit(interaction.guild_id, user.id, interaction.user.id, "xp_changed", f"{old} -> {new}")
        await reply(interaction, f"XP: {old} → {new}. Уровень: {level_for_xp(new)}.")

    @xp.command(name="add", description="Добавить опыт участнику")
    @staff_check()
    async def xp_add(self, interaction: discord.Interaction, user: discord.Member, amount: app_commands.Range[int, 1, MAX_XP]):
        await self.adjust(interaction, user, amount, "add")

    @xp.command(name="remove", description="Уменьшить опыт участника")
    @staff_check()
    async def xp_remove(self, interaction: discord.Interaction, user: discord.Member, amount: app_commands.Range[int, 1, MAX_XP]):
        await self.adjust(interaction, user, -amount, "add")

    @xp.command(name="set", description="Установить общий опыт участника")
    @staff_check()
    async def xp_set(self, interaction: discord.Interaction, user: discord.Member, amount: app_commands.Range[int, 0, MAX_XP]):
        await self.adjust(interaction, user, amount, "set")

    @levels.command(name="set", description="Установить уровень 0–1000")
    @staff_check()
    async def level_set(self, interaction: discord.Interaction, user: discord.Member, level: app_commands.Range[int, 0, 1000]):
        await self.adjust(interaction, user, xp_for_level(level), "set")

    @levels.command(name="sync", description="Исправить роли одного участника")
    @staff_check()
    async def sync(self, interaction: discord.Interaction, user: discord.Member):
        await guard(interaction, user, permission="manage_roles")
        guard_assigned_roles(interaction)
        await interaction.response.defer(ephemeral=True)
        async with self.bot.member_lock(interaction.guild_id, user.id):
            state = await self.bot.db.state(interaction.guild_id, user.id)
            if not state:
                return await reply(interaction, "Участник ещё не зарегистрирован в системе проверки.")
            ok = await self.bot.roles.apply(user, verified=state["status"] == "verified")
            await self.bot.audit(interaction.guild_id, user.id, interaction.user.id, "roles_sync", str(ok))
        await reply(interaction, "Роли синхронизированы." if ok else "Есть ошибки ролей; см. /modlog.")

    @levels.command(name="syncall", description="Запустить последовательное исправление ролей сервера")
    @staff_check()
    async def syncall(self, interaction: discord.Interaction):
        # A bulk operation has no single target; restrict it to server admins/owner.
        if not (interaction.user.guild_permissions.administrator or interaction.user.id == interaction.guild.owner_id):
            raise app_commands.CheckFailure("Массовая синхронизация доступна владельцу и администраторам.")
        await guard(interaction, permission="manage_roles")
        guard_assigned_roles(interaction)
        if self.bot.repair_locks[interaction.guild_id].locked():
            return await reply(interaction, "Синхронизация уже выполняется.")
        self.bot.spawn(self.bot.repair_guild(interaction.guild, force=True))
        await self.bot.audit(interaction.guild_id, 0, interaction.user.id, "syncall_requested")
        await reply(interaction, "Синхронизация запущена. Результат появится в журнале модерации.")
