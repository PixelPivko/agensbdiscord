import discord
from discord import app_commands
from discord.ext import commands

from cogs.common import guard, reason_text, reply, staff_check
from services.messages import event_title, event_detail, participant_label, verification_label


@app_commands.guild_only()
class Moderation(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(description="Выдать предупреждение")
    @staff_check()
    async def warn(self, interaction: discord.Interaction, user: discord.Member, reason: str):
        reason = reason_text(reason)
        await guard(interaction, user)
        await interaction.response.defer(ephemeral=True)
        async with self.bot.member_lock(interaction.guild_id, user.id):
            warning_id, count = await self.bot.moderation.warn(user, interaction.user, reason)
        await reply(interaction, f"Записал предупреждение #{warning_id}. Сейчас активных: {count}. Надеюсь, дальше без приключений.")

    @app_commands.command(description="Показать последние 20 активных предупреждений")
    @staff_check()
    async def warnings(self, interaction: discord.Interaction, user: discord.Member):
        rows = await self.bot.db.rows("SELECT id,reason,source FROM warnings WHERE guild_id=? AND user_id=? "
                                       "AND active=1 ORDER BY id DESC LIMIT 20", (interaction.guild_id, user.id))
        await reply(interaction, "\n".join(f"#{r['id']} [{r['source']}] {discord.utils.escape_markdown(r['reason'])}"
                                           for r in rows) or "В журнале чисто: активных предупреждений нет!")

    @app_commands.command(description="Убрать предупреждение из журнала по номеру")
    @staff_check()
    async def unwarn(self, interaction: discord.Interaction, warning_id: int):
        await interaction.response.defer(ephemeral=True)
        rows = await self.bot.db.rows("SELECT user_id FROM warnings WHERE guild_id=? AND id=? AND active=1",
                                       (interaction.guild_id, warning_id))
        if not rows:
            return await reply(interaction, "Такого активного предупреждения у нас не нашлось. Проверь номер.")
        user_id = rows[0]["user_id"]
        try:
            member = await interaction.guild.fetch_member(user_id)
        except discord.NotFound:
            member = None
        if member:
            await guard(interaction, member)
        async with self.bot.member_lock(interaction.guild_id, user_id):
            await self.bot.db.execute("UPDATE warnings SET active=0 WHERE guild_id=? AND id=?",
                                       (interaction.guild_id, warning_id))
            await self.bot.audit(interaction.guild_id, user_id, interaction.user.id, "warning_removed", str(warning_id))
        await reply(interaction, "Предупреждение убрал. Если есть timeout, его нужно снимать отдельно.")

    @app_commands.command(description="Очистить список предупреждений участника")
    @staff_check()
    async def clearwarns(self, interaction: discord.Interaction, user: discord.Member):
        await guard(interaction, user)
        await interaction.response.defer(ephemeral=True)
        async with self.bot.member_lock(interaction.guild_id, user.id):
            count = await self.bot.db.execute("UPDATE warnings SET active=0 WHERE guild_id=? AND user_id=? AND active=1",
                                               (interaction.guild_id, user.id))
            await self.bot.audit(interaction.guild_id, user.id, interaction.user.id, "warnings_cleared", str(count))
        await reply(interaction, f"Прибрались в журнале: снято предупреждений — {count}.")

    @app_commands.command(description="Ограничить участника на заданное число минут")
    @staff_check()
    async def timeout(self, interaction: discord.Interaction, user: discord.Member,
                      minutes: app_commands.Range[int, 1, 40320], reason: str):
        reason = reason_text(reason)
        await guard(interaction, user, permission="moderate_members", timeout=True)
        await interaction.response.defer(ephemeral=True)
        async with self.bot.member_lock(interaction.guild_id, user.id):
            await self.bot.moderation.timeout(user, minutes, reason)
            await self.bot.audit(interaction.guild_id, user.id, interaction.user.id, "timeout", f"{minutes} min; {reason}")
        await reply(interaction, "Пауза в чате включена: timeout установлен.")

    @app_commands.command(description="Снять timeout модерации; проверка captcha остаётся обязательной")
    @staff_check()
    async def untimeout(self, interaction: discord.Interaction, user: discord.Member, reason: str):
        reason = reason_text(reason)
        await guard(interaction, user, permission="moderate_members", timeout=True)
        await interaction.response.defer(ephemeral=True)
        async with self.bot.member_lock(interaction.guild_id, user.id):
            await self.bot.moderation.timeout(user, 0, reason)
            await self.bot.audit(interaction.guild_id, user.id, interaction.user.id, "untimeout", reason)
        await reply(interaction, "Паузу от модерации снял. Если антибот-проверка ещё не пройдена, её ограничение остаётся.")

    @app_commands.command(description="Исключить участника")
    @staff_check()
    async def kick(self, interaction: discord.Interaction, user: discord.Member, reason: str):
        reason = reason_text(reason)
        await guard(interaction, user, permission="kick_members")
        await interaction.response.defer(ephemeral=True)
        await user.kick(reason=reason)
        await self.bot.audit(interaction.guild_id, user.id, interaction.user.id, "kick", reason)
        await reply(interaction, "Готово: участник исключён с сервера. Решение записал в журнал.")

    @app_commands.command(description="Заблокировать участника без удаления истории сообщений")
    @staff_check()
    async def ban(self, interaction: discord.Interaction, user: discord.Member, reason: str):
        reason = reason_text(reason)
        await guard(interaction, user, permission="ban_members")
        await interaction.response.defer(ephemeral=True)
        await user.ban(reason=reason, delete_message_seconds=0)
        await self.bot.audit(interaction.guild_id, user.id, interaction.user.id, "ban", reason)
        await reply(interaction, "Готово: участник заблокирован. Решение записал в журнал.")

    @app_commands.command(description="Снять блокировку по Discord ID")
    @staff_check()
    async def unban(self, interaction: discord.Interaction, user_id: str, reason: str):
        reason = reason_text(reason)
        if not user_id.isascii() or not user_id.isdecimal() or not 0 < int(user_id) < 2**63:
            return await reply(interaction, "Нужен числовой Discord ID. Скопируй его у участника и попробуй ещё раз.")
        await guard(interaction, permission="ban_members")
        await interaction.response.defer(ephemeral=True)
        target = discord.Object(id=int(user_id))
        await interaction.guild.unban(target, reason=reason)
        await self.bot.audit(interaction.guild_id, target.id, interaction.user.id, "unban", reason)
        await reply(interaction, "Блокировку снял — участник снова может зайти по приглашению.")

    @app_commands.command(description="Удалить до 100 последних сообщений текущего канала")
    @staff_check()
    async def purge(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100]):
        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return await reply(interaction, "С метлой работаю в текстовом канале. Позови меня туда!")
        permissions = channel.permissions_for(interaction.guild.me)
        if not permissions.manage_messages or not permissions.read_message_history:
            raise app_commands.CheckFailure("Метла есть, допуска нет: нужны Manage Messages и Read Message History в этом канале.")
        if not channel.permissions_for(interaction.user).view_channel:
            raise app_commands.CheckFailure("В этот канал у тебя пока нет доступа.")
        await interaction.response.defer(ephemeral=True)
        deleted = await channel.purge(limit=amount, reason=f"SecurityAgent: staff {interaction.user.id}")
        await self.bot.audit(interaction.guild_id, 0, interaction.user.id, "purge", f"channel={channel.id}; count={len(deleted)}")
        await reply(interaction, f"Подмёл чат: удалено сообщений — {len(deleted)}.")

    @app_commands.command(description="Информация об участнике")
    @staff_check()
    async def userinfo(self, interaction: discord.Interaction, user: discord.Member):
        state = await self.bot.db.state(interaction.guild_id, user.id)
        joined = user.joined_at.isoformat() if user.joined_at else "неизвестно"
        await reply(interaction, f"ID: {user.id}\nАккаунт создан: {user.created_at.date()}\n"
                    f"Вступление: {joined}\nПропуск: {verification_label(state['status'] if state else None)}\n"
                    f"Уровень: {await self.bot.db.level(interaction.guild_id, user.id)}")

    @app_commands.command(description="Заглянуть в журнал дежурного: последние 15 событий")
    @staff_check()
    async def modlog(self, interaction: discord.Interaction, user: discord.Member | None = None):
        sql = "SELECT * FROM moderation_actions WHERE guild_id=?"
        params = [interaction.guild_id]
        if user:
            sql += " AND user_id=?"
            params.append(user.id)
        rows = await self.bot.db.rows(sql + " ORDER BY id DESC LIMIT 15", params)
        await reply(interaction, "\n".join(f"#{r['id']} {event_title(r['action'])} · {participant_label(r['user_id'])} · "
                                           f"{discord.utils.escape_markdown(event_detail(r['action'], r['detail'])[:120])}" for r in rows)
                    or "В журнале пока тихо. Дежурный пьёт чай.")
