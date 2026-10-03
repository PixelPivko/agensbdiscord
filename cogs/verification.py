import time
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from cogs.common import embed, guard, reply, staff_check


class CaptchaModal(discord.ui.Modal, title="ПОДТВЕРДИТЕ, ЧТО ВЫ НЕ БОТ"):
    def __init__(self, service, guild_id, user_id, session):
        super().__init__(timeout=600)
        self.service, self.guild_id, self.user_id = service, guild_id, user_id
        self.nonce = session["nonce"]
        self.answer_field = discord.ui.TextInput(label=session["question"], min_length=1, max_length=4)
        self.add_item(self.answer_field)

    async def on_submit(self, interaction):
        if interaction.user.id != self.user_id:
            return await reply(interaction, "Эта проверка принадлежит другому участнику.")
        await interaction.response.defer(ephemeral=True)
        await self.service.submit(interaction, self.guild_id, self.nonce, self.answer_field.value)

    async def on_error(self, interaction, error):
        await self.service.bot.audit(self.guild_id, self.user_id, 0, "captcha_error", type(error).__name__)
        await reply(interaction, "Не удалось завершить проверку. Повторите позже; сотрудник увидит ошибку.")


class VerificationView(discord.ui.View):
    def __init__(self, service, guild_id):
        super().__init__(timeout=None)
        self.service, self.guild_id = service, guild_id
        button = discord.ui.Button(label="Ответить / начать проверку", style=discord.ButtonStyle.primary,
                                   custom_id=f"security:verify:v1:{guild_id}")
        button.callback = self.open_modal
        self.add_item(button)

    async def open_modal(self, interaction):
        if not self.service.bot.operational:
            return await reply(interaction, "Бот ещё выполняет проверку запуска.")
        if interaction.guild_id and interaction.guild_id != self.guild_id:
            return await reply(interaction, "Эта кнопка относится к другому серверу.")
        guild = self.service.bot.get_guild(self.guild_id)
        if guild is None:
            return await reply(interaction, "Сервер временно недоступен.")
        # Cache avoids network before the modal's three-second response deadline.
        member = guild.get_member(interaction.user.id)
        if member is None:
            return await reply(interaction, "Вы должны быть участником этого сервера. Повторите после загрузки списка.")
        async with self.service.bot.member_lock(self.guild_id, member.id):
            state = await self.service.bot.db.state(self.guild_id, member.id)
            if not state:
                return await reply(interaction, "Попросите сотрудника начать проверку через /verification reset.")
            if state["status"] in {"verified", "passed"}:
                return await reply(interaction, "Captcha уже пройдена. Незавершённая выдача доступа будет повторена.")
            cfg = self.service.bot.cfg(self.guild_id)["verification"]
            try:
                session = await self.service.bot.db.challenge(self.guild_id, member.id,
                                                              cfg["captcha_ttl_seconds"], cfg["max_attempts"])
            except ValueError as exc:
                return await reply(interaction, str(exc))
            await interaction.response.send_modal(CaptchaModal(self.service, self.guild_id, member.id, session))

    async def on_error(self, interaction, error, item):
        await self.service.bot.audit(self.guild_id, interaction.user.id, 0, "verification_view_error",
                                     type(error).__name__)
        await reply(interaction, "Проверка временно недоступна. Повторите позже.")


class Verification(commands.Cog):
    verification = app_commands.Group(name="verification", description="Проверка участников", guild_only=True)

    def __init__(self, bot):
        self.bot = bot

    def register_views(self):
        for guild_id in self.bot.config["guilds"]:
            self.bot.add_view(VerificationView(self, guild_id))

    async def restrict(self, member):
        gid, uid = member.guild.id, member.id
        await self.bot.db.ensure_user(gid, uid)
        state = await self.bot.db.state(gid, uid)
        # Preserve moderation timeout already present before the verification timeout is applied.
        current = member.timed_out_until.timestamp() if member.timed_out_until else 0
        known = state["timeout_until"]
        if current > time.time() and abs(current-known) > 2:
            await self.bot.db.execute("UPDATE verification SET moderation_until=MAX(moderation_until,?) "
                                      "WHERE guild_id=? AND user_id=?", (current, gid, uid))
        until = time.time() + 7 * 86400
        await self.bot.db.execute("UPDATE verification SET timeout_until=? WHERE guild_id=? AND user_id=?",
                                  (until, gid, uid))
        try:
            await member.timeout(datetime.fromtimestamp(max(until, current), timezone.utc),
                                 reason="SecurityAgent: pending captcha")
            await self.bot.audit(gid, uid, 0, "verification_timeout", "7 days; removed after captcha")
        except discord.HTTPException as exc:
            await self.bot.audit(gid, uid, 0, "bot_permission_error", f"Verification timeout: {type(exc).__name__}")
        await self.bot.roles.apply(member, verified=False)

    async def deliver(self, member, *, force=False):
        gid, uid = member.guild.id, member.id
        cfg = self.bot.cfg(gid)
        state = await self.bot.db.state(gid, uid)
        now = time.time()
        if not state or state["status"] != "pending":
            return "Captcha не требуется."
        if not force and now - state["last_sent"] < cfg["verification"]["resend_cooldown_seconds"]:
            return "Повторная отправка доступна через минуту."
        await self.bot.db.execute("UPDATE verification SET last_sent=? WHERE guild_id=? AND user_id=?", (now, gid, uid))
        session = await self.bot.db.challenge(gid, uid, cfg["verification"]["captcha_ttl_seconds"],
                                               cfg["verification"]["max_attempts"])
        message = embed("**ПОДТВЕРДИТЕ, ЧТО ВЫ НЕ БОТ**\nПеред доступом к серверу пройдите проверку.\n\n"
                        f"Решите пример: **{session['question']}**\nНажмите «Ответить». "
                        f"Срок проверки: {cfg['verification']['captcha_ttl_seconds']} секунд.",
                        color=0xF0B232)
        try:
            await member.send(embed=message, view=VerificationView(self, gid))
            return "Captcha отправлена в личные сообщения."
        except discord.HTTPException:
            channel = member.guild.get_channel(cfg["verification_channel_id"])
            if channel:
                try:
                    await channel.send(content=f"<@{uid}>", embed=embed(
                        "Не удалось отправить личное сообщение. Разрешите личные сообщения от участников сервера "
                        "и попросите сотрудника выполнить `/verification resend`. Пока действует timeout, "
                        "взаимодействие в сервере ограничено. После истечения timeout можно использовать кнопку ниже.",
                        color=0xF0B232), view=VerificationView(self, gid),
                        allowed_mentions=discord.AllowedMentions(users=[member]))
                except discord.HTTPException as exc:
                    await self.bot.audit(gid, uid, 0, "verification_delivery_error", type(exc).__name__)
            await self.bot.audit(gid, uid, 0, "verification_dm_closed", "Fallback instructions requested")
            return "DM закрыты. Инструкция отправлена в канал проверки, если он доступен."

    async def complete(self, member):
        gid, uid = member.guild.id, member.id
        state = await self.bot.db.state(gid, uid)
        if not state or state["status"] != "passed":
            return False
        # Fetch fresh Discord state so a concurrent staff-issued timeout is not silently removed.
        member = await member.guild.fetch_member(uid)
        current = member.timed_out_until.timestamp() if member.timed_out_until else 0
        until = state["moderation_until"]
        if current > time.time() and abs(current - state["timeout_until"]) > 2:
            until = max(until, current)
        await member.timeout(datetime.fromtimestamp(until, timezone.utc) if until > time.time() else None,
                             reason="SecurityAgent: captcha passed; remove verification timeout")
        await self.bot.db.mark_verified(gid, uid)
        roles_ok = await self.bot.roles.apply(member, verified=True)
        await self.bot.audit(gid, uid, 0, "captcha_passed", "Roles synchronized" if roles_ok else "Role repair required")
        return roles_ok

    async def submit(self, interaction, guild_id, nonce, answer):
        guild = self.bot.get_guild(guild_id)
        member = await guild.fetch_member(interaction.user.id) if guild else None
        if member is None:
            return await reply(interaction, "Вы больше не участвуете в сервере.")
        async with self.bot.member_lock(guild_id, member.id):
            cfg = self.bot.cfg(guild_id)["verification"]
            state = await self.bot.db.state(guild_id, member.id)
            if state and state["status"] == "verified":
                return await reply(interaction, "Вы уже прошли проверку.")
            result, remaining = await self.bot.db.answer(guild_id, member.id, nonce, answer,
                                                         cfg["captcha_ttl_seconds"], cfg["max_attempts"])
            if result != "passed":
                message = (f"❌ НЕВЕРНЫЙ ОТВЕТ\nПопробуйте ещё раз. Осталось попыток: {remaining}."
                           if result == "incorrect" else "Проверка истекла или попытки закончились. "
                           "После истечения окна проверки нажмите кнопку снова.")
                return await reply(interaction, message, color=0xED4245)
            ok = await self.complete(member)
            await reply(interaction, "✅ ПРОВЕРКА ПРОЙДЕНА\nВы успешно подтвердили, что не являетесь ботом.\n"
                        "Ограничение проверки снято. Отдельные наказания модерации сохраняются.\n"
                        + ("Доступ к серверу открыт." if ok else "Часть ролей требует исправления сотрудником."),
                        color=0x57F287)

    @commands.Cog.listener()
    async def on_member_join(self, member):
        if not self.bot.operational or member.bot or not self.bot.cfg(member.guild.id):
            return
        async with self.bot.member_lock(member.guild.id, member.id):
            state = await self.bot.db.state(member.guild.id, member.id)
            if state and state["status"] == "verified":
                await self.bot.roles.apply(member, verified=True)
                return
            if state and state["status"] == "passed":
                await self.complete(member)
                return
            await self.restrict(member)
            await self.deliver(member)

    @verification.command(name="reset", description="Сбросить проверку и отправить captcha")
    @staff_check()
    async def reset(self, interaction: discord.Interaction, user: discord.Member):
        await guard(interaction, user, permission="moderate_members", timeout=True)
        await interaction.response.defer(ephemeral=True)
        async with self.bot.member_lock(interaction.guild_id, user.id):
            await self.bot.db.reset(interaction.guild_id, user.id)
            await self.restrict(user)
            result = await self.deliver(user)
            await self.bot.audit(interaction.guild_id, user.id, interaction.user.id, "captcha_reset")
        await reply(interaction, result)

    @verification.command(name="resend", description="Повторно отправить ожидающему участнику DM captcha")
    @staff_check()
    async def resend(self, interaction: discord.Interaction, user: discord.Member):
        await guard(interaction, user)
        await interaction.response.defer(ephemeral=True)
        async with self.bot.member_lock(interaction.guild_id, user.id):
            result = await self.deliver(user)
        await reply(interaction, result)

    @verification.command(name="panel", description="Опубликовать постоянную панель в канале проверки")
    @staff_check()
    async def panel(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        channel = interaction.guild.get_channel(self.bot.cfg(interaction.guild_id)["verification_channel_id"])
        if channel is None:
            return await reply(interaction, "Канал проверки не найден.")
        await channel.send(embed=embed("Проверка участников ТОРТИК PROJECT. При активном timeout используйте "
                                      "captcha в DM. Если DM закрыты, включите их и обратитесь к сотруднику."),
                           view=VerificationView(self, interaction.guild_id))
        await reply(interaction, "Панель опубликована.")
