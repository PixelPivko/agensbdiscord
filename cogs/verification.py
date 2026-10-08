import asyncio
import time
from types import SimpleNamespace
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from cogs.common import embed, guard, reply, staff_check
from services.manual_verify import manually_verify, resolve_member
from services.rules import is_staff
from services.messages import captcha_intro


class CaptchaModal(discord.ui.Modal, title="Биометрия по-тортиковски"):
    def __init__(self, service, guild_id, user_id, session):
        super().__init__(timeout=600)
        self.service, self.guild_id, self.user_id = service, guild_id, user_id
        self.nonce = session["nonce"]
        self.answer_field = discord.ui.TextInput(label=session["question"], min_length=1, max_length=4)
        self.add_item(self.answer_field)

    async def on_submit(self, interaction):
        if interaction.user.id != self.user_id:
            return await reply(interaction, "Опа, это чужой пропуск! Нажми кнопку в своей проверке.")
        await interaction.response.defer(ephemeral=True)
        await self.service.submit(interaction, self.guild_id, self.nonce, self.answer_field.value)

    async def on_error(self, interaction, error):
        await self.service.bot.audit(self.guild_id, self.user_id, 0, "captcha_error", type(error).__name__)
        await reply(interaction, "Сканер зажевал бумажку. Попробуй чуть позже — дежурным уже оставил запись об ошибке.")


class VerificationView(discord.ui.View):
    def __init__(self, service, guild_id):
        super().__init__(timeout=None)
        self.service, self.guild_id = service, guild_id
        button = discord.ui.Button(label="Пройти биометрию", style=discord.ButtonStyle.success,
                                   custom_id=f"security:verify:v1:{guild_id}")
        button.callback = self.open_modal
        self.add_item(button)

    async def open_modal(self, interaction):
        if not self.service.bot.operational:
            return await reply(interaction, "Дежурный ещё открывает КПП. Дай мне немного времени и повтори команду.")
        if interaction.guild_id and interaction.guild_id != self.guild_id:
            return await reply(interaction, "Кажется, это пропуск на другой сервер. Открой его проверку.")
        guild = self.service.bot.get_guild(self.guild_id)
        if guild is None:
            return await reply(interaction, "До сервера пока не достучался. Попробуй чуть позже.")
        # Cache avoids network before the modal's three-second response deadline.
        member = guild.get_member(interaction.user.id)
        if member is None:
            return await reply(interaction, "Пока не вижу тебя в списке гостей. Убедись, что зашёл на сервер, и попробуй чуть позже.")
        async with self.service.bot.member_lock(self.guild_id, member.id):
            state = await self.service.bot.db.state(self.guild_id, member.id)
            if not state:
                return await reply(interaction, "Твоего пропуска пока нет в журнале. Попроси дежурного начать проверку через /verification reset.")
            if state["status"] in {"verified", "passed"}:
                return await reply(interaction, "Пример уже решён! Если роли ещё не доехали, я повторю выдачу — второй экзамен не нужен.")
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
        await reply(interaction, "Сканер на коротком перерыве. Нажми ещё раз чуть позже.")


class Verification(commands.Cog):
    verification = app_commands.Group(name="verification", description="КПП: пропуска и антибот-проверка", guild_only=True)

    def __init__(self, bot):
        self.bot = bot
        self.bulk_guilds = set()
        self.bulk_tasks = {}
        self.bulk_progress = {}
        self.member_deadline = 45
        self.chunk_deadline = 45

    def new_progress(self, actor_id):
        return dict(actor=actor_id, phase="ожидание сверки ролей", total=0, done=0, partial=0,
                    skipped=0, failed=0, current=0, started=time.monotonic())

    def progress_text(self, gid):
        state = self.bulk_progress.get(gid)
        if not state:
            return "Сегодня в этом запуске ещё не раздавали пропуска всем разом."
        processed = state["done"] + state["partial"] + state["skipped"] + state["failed"]
        return (f"Состояние: {state['phase']}\nОбработано: {processed}/{state['total']}\n"
                f"Роли выданы: {state['done']}; нужна сверка: {state['partial']}\n"
                f"Пропущено: {state['skipped']}; ошибок: {state['failed']}\n"
                f"Текущий ID: {state['current'] or '—'}\n"
                f"Прошло секунд: {int(time.monotonic()-state['started'])}")

    def bulk_finished(self, gid, task):
        if self.bulk_tasks.get(gid) is task:
            self.bulk_tasks.pop(gid, None)
            self.bulk_guilds.discard(gid)
            if task.cancelled():
                self.bulk_progress[gid]["phase"] = "остановлено"

    @verification.command(name="status", description="Показать прогресс массовой верификации")
    @staff_check()
    async def bulk_status(self, interaction: discord.Interaction):
        # No database query, member lock, chunk or REST member fetch on this path.
        await reply(interaction, self.progress_text(interaction.guild_id))

    @verification.command(name="stop", description="Остановить массовую верификацию")
    @staff_check()
    async def bulk_stop(self, interaction: discord.Interaction):
        gid = interaction.guild_id
        task = self.bulk_tasks.get(gid)
        if task is None or task.done():
            return await reply(interaction, "Общей раздачи сейчас нет — останавливать нечего.")
        if (interaction.user.id != self.bulk_progress[gid]["actor"]
                and interaction.user.id != interaction.guild.owner_id
                and not interaction.user.guild_permissions.administrator):
            raise app_commands.CheckFailure("Стоп-кран доступен тому, кто начал раздачу, или администратору.")
        task.cancel()
        self.bulk_progress[gid]["phase"] = "остановка"
        await reply(interaction, "Тормозим раздачу! Готовые пропуска и роли остаются на месте. "
                    "Если какой-то роли не хватило, фоновая сверка её проверит.")

    @app_commands.command(name="verify", description="Верифицировать вручную: имя, username, @участник, ID или all")
    @app_commands.describe(target="Точное имя, username, упоминание, ID участника или all для всех")
    @staff_check()
    async def verify(self, interaction: discord.Interaction, target: str):
        await interaction.response.defer(ephemeral=True)
        if target.strip().casefold() == "all":
            gid = interaction.guild_id
            if gid in self.bulk_guilds:
                return await reply(interaction, "Уже раздаю пропуска! Загляни в /verification status, вторую очередь не открываем.")
            self.bulk_guilds.add(gid)
            self.bulk_progress[gid] = self.new_progress(interaction.user.id)
            try:
                # Return a visible response before starting any gateway/API or database work.
                await reply(interaction, "Открываю общую раздачу пропусков! Прогресс: `/verification status`. "
                            "Стоп-кран: `/verification stop`. Роли выдаю по очереди — Discord не любит суету, это займёт время.")
                await self.bot.audit(gid, 0, interaction.user.id, "verify_all_started")
                task = self.bot.spawn(self.verify_all(interaction.guild, interaction.user.id))
                self.bulk_tasks[gid] = task
                task.add_done_callback(lambda finished: self.bulk_finished(gid, finished))
            except BaseException:
                self.bulk_guilds.discard(gid)
                raise
            return
        if not interaction.guild.chunked:
            async with asyncio.timeout(self.chunk_deadline):
                await interaction.guild.chunk(cache=True)
        member = resolve_member(interaction.guild.members, target)
        member = await interaction.guild.fetch_member(member.id)
        actor = await interaction.guild.fetch_member(interaction.user.id)
        context = SimpleNamespace(guild=interaction.guild, guild_id=interaction.guild_id,
                                  user=actor, client=self.bot)
        async with self.bot.member_lock(interaction.guild_id, member.id):
            ok = await manually_verify(self.bot, context, member)
        await reply(interaction, f"Пропуск участнику {member.id} оформлен вручную! "
                    + ("Стартовые роли и роли уровня уже на месте." if ok else "Часть ролей застряла по дороге. Подробности для дежурных — /modlog."))

    async def verify_all(self, guild, actor_id):
        state = self.bulk_progress.setdefault(guild.id, self.new_progress(actor_id))
        try:
            async with self.bot.repair_locks[guild.id]:
                state["phase"] = "загрузка участников"
                if not guild.chunked:
                    async with asyncio.timeout(self.chunk_deadline):
                        await guild.chunk(cache=True)
                members = [m.id for m in guild.members if not m.bot]
                state["total"] = len(members)
                state["phase"] = "выдача ролей"
                for uid in members:
                    state["current"] = uid
                    try:
                        async with asyncio.timeout(self.member_deadline):
                            actor = await guild.fetch_member(actor_id)
                            if not is_staff(actor, self.bot.cfg(guild.id)):
                                state["phase"] = "остановлено: staff-доступ отозван"
                                break
                            member = await guild.fetch_member(uid)
                            context = SimpleNamespace(guild=guild, guild_id=guild.id, user=actor, client=self.bot)
                            async with self.bot.member_lock(guild.id, uid):
                                ok = await manually_verify(self.bot, context, member)
                            state["done"] += int(ok)
                            state["partial"] += int(not ok)
                    except app_commands.CheckFailure as exc:
                        state["skipped"] += 1
                        await self.bot.audit(guild.id, uid, actor_id, "manual_verify_skipped", str(exc))
                    except TimeoutError:
                        state["failed"] += 1
                        state["phase"] = "остановлено: Discord не ответил за 45 секунд"
                        await self.bot.audit(guild.id, uid, actor_id, "verify_all_timeout", "Stopped; inspect status before retry")
                        break
                    except discord.HTTPException as exc:
                        state["failed"] += 1
                        state["phase"] = "остановлено: ошибка Discord"
                        await self.bot.audit(guild.id, uid, actor_id, "manual_verify_error", type(exc).__name__)
                        break
                    await asyncio.sleep(max(1, self.bot.cfg(guild.id)["levels"]["sync_delay_seconds"]))
                else:
                    state["phase"] = "завершено"
        except asyncio.CancelledError:
            state["phase"] = "остановлено"
            raise
        except Exception as exc:
            state["failed"] += 1
            state["phase"] = "остановлено: ошибка"
            await self.bot.audit(guild.id, 0, actor_id, "verify_all_error", type(exc).__name__)
        finally:
            self.bulk_guilds.discard(guild.id)
            await self.bot.audit(guild.id, 0, actor_id, "verify_all_finished",
                                 f"roles_ok={state['done']}; role_repair={state['partial']}; "
                                 f"skipped={state['skipped']}; errors={state['failed']}; {state['phase']}")

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
            return "Уже свой человек — новый пример не нужен."
        if not force and now - state["last_sent"] < cfg["verification"]["resend_cooldown_seconds"]:
            return "Уже стучался в личку! Дай минутку перед повторной отправкой."
        await self.bot.db.execute("UPDATE verification SET last_sent=? WHERE guild_id=? AND user_id=?", (now, gid, uid))
        session = await self.bot.db.challenge(gid, uid, cfg["verification"]["captcha_ttl_seconds"],
                                               cfg["verification"]["max_attempts"])
        message = embed(captcha_intro(session['question'], cfg['verification']['captcha_ttl_seconds']),
                        color=0xF2B66D, title="О, человек на КПП!")
        try:
            await member.send(embed=message, view=VerificationView(self, gid))
            return "Постучался в личку с примером — пусть проверит ЛС!"
        except discord.HTTPException:
            channel = member.guild.get_channel(cfg["verification_channel_id"])
            if channel:
                try:
                    await channel.send(content=f"<@{uid}>", embed=embed(
                        "Тук-тук, а личка закрыта! Я дежурный антибот-системы ТОРТИК. "
                        "Открой ЛС от участников сервера и попроси сотрудника отправить пример через "
                        "`/verification resend`. Пока действует timeout, проходить проверку нужно в ЛС. "
                        "Кнопка ниже пригодится, когда Discord разрешает взаимодействие в сервере.",
                        color=0xF0B232), view=VerificationView(self, gid),
                        allowed_mentions=discord.AllowedMentions(users=[member]))
                except discord.HTTPException as exc:
                    await self.bot.audit(gid, uid, 0, "verification_delivery_error", type(exc).__name__)
            await self.bot.audit(gid, uid, 0, "verification_dm_closed", "Fallback instructions requested")
            return "В личку не пустили. Попробовал оставить подсказку в канале КПП; ошибки доставки будут в журнале."

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
            return await reply(interaction, "Не вижу тебя на сервере. Сначала загляни к нам, потом оформим пропуск.")
        async with self.bot.member_lock(guild_id, member.id):
            cfg = self.bot.cfg(guild_id)["verification"]
            state = await self.bot.db.state(guild_id, member.id)
            if state and state["status"] == "verified":
                return await reply(interaction, "Ты уже свой, пропуск на руках! Повторно сдавать математику не надо.")
            result, remaining = await self.bot.db.answer(guild_id, member.id, nonce, answer,
                                                         cfg["captcha_ttl_seconds"], cfg["max_attempts"])
            if result != "passed":
                message = (f"Упс, цифры не сошлись 😅\nНичего страшного, попробуй ещё! Осталось попыток: {remaining}. "
                           "Если они закончились, дождись окончания срока примера и нажми кнопку снова."
                           if result == "incorrect" else "Этот пример уже ушёл на обед или попытки закончились. "
                           "Когда срок примера закончится, нажми кнопку — принесу новый. За ошибки не наказываем.")
                return await reply(interaction, message, color=0xED4245)
            ok = await self.complete(member)
            await reply(interaction, "✅ Биометрия сошлась, свой человек!\n"
                        "Ограничение антибот-проверки снято. Если был отдельный timeout от модератора, он остаётся.\n"
                        + ("Роли выданы — залетай в ТОРТИК, располагайся!" if ok else
                           "Пропуск готов, но часть ролей не выдалась. Дежурные увидят ошибку в журнале."),
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

    @verification.command(name="resend", description="Ещё раз постучаться в ЛС с примером")
    @staff_check()
    async def resend(self, interaction: discord.Interaction, user: discord.Member):
        await guard(interaction, user)
        await interaction.response.defer(ephemeral=True)
        async with self.bot.member_lock(interaction.guild_id, user.id):
            result = await self.deliver(user)
        await reply(interaction, result)

    @verification.command(name="panel", description="Повесить табличку КПП с кнопкой проверки")
    @staff_check()
    async def panel(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        channel = interaction.guild.get_channel(self.bot.cfg(interaction.guild_id)["verification_channel_id"])
        if channel is None:
            return await reply(interaction, "Не нашёл наш КПП. Попроси администратора проверить канал в настройках.")
        await channel.send(embed=embed("**Добро пожаловать на КПП ТОРТИК!**\n"
                                      "Здесь отсеиваем ботов и спам-налёты, а людям выдаём пропуска. "
                                      "Наша «биометрия» — обычный пример, никаких фото и отпечатков.\n\n"
                                      "С активным timeout загляни в ЛС: там кнопка проверки. "
                                      "ЛС закрыты? Открой их и попроси сотрудника выполнить `/verification resend`. "
                                      "Если Discord уже разрешает нажимать кнопки в сервере — начинай ниже."),
                           view=VerificationView(self, interaction.guild_id))
        await reply(interaction, "Табличка КПП повешена, кнопка на месте!")
