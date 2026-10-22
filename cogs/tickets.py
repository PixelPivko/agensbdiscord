"""Private, persistent ticket panels and restart-safe multi-page forms."""
import json
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands
from cogs.common import reply, staff_check


LOG = logging.getLogger('security.tickets')
KINDS = {
 'player': 'Жалоба на игрока', 'admin': 'Жалоба на администратора',
 'appeal': 'Апелляция', 'vmn': 'Апелляция на В.М.Н',
 'mrp': 'Заявка в администрацию Medium RP',
 'classic': 'Заявка в администрацию Classic',
 'discord': 'Заявка в администрацию Discord',
}
RECRUIT = ('**Набор в администрацию — от 13 лет.**\n'
           'От 200 часов в игре (можно меньше — готовы предоставить всё обучение).\n'
           'Знание админ-панели желательно.\n\n'
           '**От нас:** хороший коллектив, быстрый карьерный рост. '
           'Можете указывать участие в проекте в своём опыте работы; '
           'это не обещание официального трудоустройства.\n\n'
           'Почему именно ты? Если трудно сформулировать — «потому что я крутой» тоже начало 🙂')

def questions(kind):
    base = ([] if kind in ('mrp', 'classic', 'discord') else ['Сервер: MRP / CLASSIC / DISCORD']) + ['Ваш никнейм', 'Ваш SteamID (для Discord — ID)']
    if kind in ('player', 'admin'):
        return base + ['Суть жалобы', 'Описание всей ситуации', 'Согласны с наказанием за ложь? Y/N']
    if kind in ('appeal', 'vmn'):
        return base + ['Причина бана', 'Описание всей ситуации', 'Почему снять или смягчить наказание?',
                       'Согласны с X2 наказанием за ложь? Y/N']
    result = base + ['Ваш возраст', 'Часы в игре и опыт с админ-панелью', 'Почему взять именно вас?']
    if kind == 'mrp':
        result += ['Что такое ГОК?', 'Что такое ПХ?', 'Год основания ПХ', 'Что такое CAP-5?', 'Что такое CAP-1?']
    return result


def server_for(kind, answers):
    if kind in ('mrp', 'classic', 'discord'):
        return kind
    value = answers[0].strip().lower() if answers else ''
    if value not in ('mrp', 'classic', 'discord'):
        raise ValueError('Укажи сервер: MRP, CLASSIC или DISCORD.')
    return value


def reviewer_roles(cfg, kind, server):
    routing = cfg['routing']
    if kind == 'vmn':
        return set(routing['vmn'])
    if kind == 'admin':
        return set(routing['admin'][server])
    return set(routing['common']) | set(routing['servers'][server])


def can_review(member, row, cfg):
    if member.id == row['user_id']:
        return False
    if member.id == member.guild.owner_id or member.guild_permissions.administrator:
        return True
    try:
        permitted = reviewer_roles(cfg, row['kind'], server_for(row['kind'], json.loads(row['answers'])))
    except (KeyError, ValueError):
        return False
    return any(r.id in permitted for r in member.roles)

SCHEMA = '''CREATE TABLE IF NOT EXISTS tickets(
 guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, kind TEXT NOT NULL,
 channel_id INTEGER NOT NULL DEFAULT 0, answers TEXT NOT NULL DEFAULT '[]',
 status TEXT NOT NULL DEFAULT 'draft', claimed_by INTEGER NOT NULL DEFAULT 0,
 updated_at REAL NOT NULL, PRIMARY KEY(guild_id,user_id));'''

class SafeView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    async def interaction_check(self, i):
        if not i.guild or not self.cog.bot.operational or not self.cog.settings(i.guild_id):
            await reply(i, 'Тикетная пока закрыта. Попробуй чуть позже.')
            return False
        return True

    async def on_error(self, i, error, item):
        LOG.error('ticket_ui_error type=%s', type(error).__name__)
        await reply(i, 'Не получилось выполнить действие. Проверьте права бота и повторите; анкета сохранена.')

class Panel(SafeView):
    def __init__(self, cog):
        super().__init__(cog)
        for n, (key, title) in enumerate(KINDS.items()):
            button = discord.ui.Button(label=title, custom_id=f'tickets:open:{key}', row=n//2,
                                       style=discord.ButtonStyle.primary)
            async def callback(i, kind=key):
                if i.channel_id != cog.settings(i.guild_id)['panel_channel_id']:
                    return await reply(i, 'Открой анкету в канале тикетов.')
                await cog.start(i, kind)
            button.callback = callback
            self.add_item(button)

class Continue(SafeView):
    @discord.ui.button(label='Продолжить / отправить анкету', custom_id='tickets:continue', style=discord.ButtonStyle.success)
    async def resume(self, i, button):
        row = await self.cog.state(i.guild_id, i.user.id)
        if not row or row['status'] != 'draft':
            return await reply(i, 'Нажми нужную кнопку на панели тикетов.')
        answers = json.loads(row['answers'])
        if len(answers) < len(questions(row['kind'])):
            await i.response.send_modal(Form(self.cog, row['kind'], len(answers)))
        else:
            await self.cog.create(i)

class Controls(SafeView):
    @discord.ui.button(label='Взять в работу', custom_id='tickets:claim', style=discord.ButtonStyle.success)
    async def claim(self, i, button):
        await i.response.defer(ephemeral=True)
        async with self.cog.bot.member_lock(i.guild_id, i.channel_id):
            rows = await self.cog.bot.db.rows('SELECT * FROM tickets WHERE guild_id=? AND channel_id=? AND status=?',
                                            (i.guild_id, i.channel_id, 'open'))
            if not rows:
                return await reply(i, 'Открытый тикет не найден.')
            row = rows[0]
            if not can_review(i.user, row, self.cog.settings(i.guild_id)):
                return await reply(i, 'Этот тикет доступен только ответственным за его направление.')
            if row['claimed_by']:
                return await reply(i, f"Тикет уже взял <@{row['claimed_by']}>.")
            await self.cog.bot.db.execute('UPDATE tickets SET claimed_by=? WHERE guild_id=? AND channel_id=?',
                                         (i.user.id, i.guild_id, i.channel_id))
            await i.channel.send(f'🛠 Взял в работу: {i.user.mention}', allowed_mentions=discord.AllowedMentions.none())
            await reply(i, 'Тикет твой. Помоги человеку разобраться 🙂')

    @discord.ui.button(label='Закрыть тикет', custom_id='tickets:close', style=discord.ButtonStyle.secondary)
    async def close_ticket(self, i, button):
        await i.response.defer(ephemeral=True)
        async with self.cog.bot.member_lock(i.guild_id, i.channel_id):
            rows = await self.cog.bot.db.rows('SELECT * FROM tickets WHERE guild_id=? AND channel_id=? AND status=?',
                                            (i.guild_id, i.channel_id, 'open'))
            if not rows:
                return await reply(i, 'Тикет уже закрыт.')
            if not can_review(i.user, rows[0], self.cog.settings(i.guild_id)):
                return await reply(i, 'Закрыть тикет могут ответственные за его направление.')
            member = i.guild.get_member(rows[0]['user_id'])
            if member is None:
                try:
                    member = await i.guild.fetch_member(rows[0]['user_id'])
                except discord.NotFound:
                    member = None
            if member:
                overwrite = i.channel.overwrites_for(member)
                overwrite.send_messages = False
                overwrite.send_messages_in_threads = False
                await i.channel.set_permissions(member, overwrite=overwrite, reason='Тикет закрыт')
            await self.cog.bot.db.execute("UPDATE tickets SET status='closed' WHERE guild_id=? AND channel_id=?",
                                         (i.guild_id, i.channel_id))
            await i.channel.send('✅ Тикет закрыт. Переписка сохранена, новые обращения — через панель.', view=None)
            await reply(i, 'Закрыто без удаления переписки.')

class Form(discord.ui.Modal):
    def __init__(self, cog, kind, offset):
        super().__init__(title=KINDS[kind][:45], timeout=900)
        self.cog, self.kind, self.offset = cog, kind, offset
        for q in questions(kind)[offset:offset+5]:
            self.add_item(discord.ui.TextInput(label=q, style=discord.TextStyle.paragraph, max_length=900))

    async def on_submit(self, i):
        await i.response.defer(ephemeral=True)
        async with self.cog.bot.member_lock(i.guild_id, i.user.id):
            row = await self.cog.state(i.guild_id, i.user.id)
            if not row or row['status'] != 'draft' or row['kind'] != self.kind or len(json.loads(row['answers'])) != self.offset:
                return await reply(i, 'Эта форма устарела. Продолжи через панель тикетов.')
            values = [str(x).strip() for x in self.children]
            if any(not x for x in values):
                return await reply(i, 'Заполни все поля, пожалуйста. Открой форму заново через панель.')
            qs = questions(self.kind)[self.offset:self.offset+5]
            for q, value in zip(qs, values):
                if q.startswith('Сервер:') and value.lower() not in ('mrp', 'classic', 'discord'):
                    return await reply(i, 'В поле сервера укажи MRP, CLASSIC или DISCORD. Открой форму повторно.')
                if q.endswith('Y/N') and value.upper() not in ('Y', 'N'):
                    return await reply(i, 'В согласии укажи Y или N. Открой форму ещё раз.')
                if q == 'Ваш возраст' and (not value.isdecimal() or not 13 <= int(value) <= 120):
                    return await reply(i, 'Набор доступен с 13 лет. Возраст укажи числом.')
            answers = json.loads(row['answers']) + values
            await self.cog.bot.db.execute('UPDATE tickets SET answers=?,updated_at=? WHERE guild_id=? AND user_id=?',
                                         (json.dumps(answers, ensure_ascii=False), time.time(), i.guild_id, i.user.id))
        if len(answers) == len(questions(self.kind)):
            return await self.cog.create(i)
        await i.followup.send('Анкета сохранена. Нажми ниже, чтобы продолжить или отправить её администрации.',
                              view=Continue(self.cog), ephemeral=True)

    async def on_error(self, i, error):
        LOG.error('ticket_form_error type=%s', type(error).__name__)
        await reply(i, 'Анкету не удалось сохранить. Попробуй ещё раз через панель.')

class Tickets(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def settings(self, gid):
        return (self.bot.cfg(gid) or {}).get('tickets')

    async def cog_load(self):
        await self.bot.db.execute(SCHEMA)
        await self.bot.db.execute('CREATE TABLE IF NOT EXISTS ticket_panels(guild_id INTEGER PRIMARY KEY,message_id INTEGER NOT NULL)')
        for cls in (Panel, Continue, Controls):
            self.bot.add_view(cls(self))

    async def state(self, gid, uid):
        rows = await self.bot.db.rows('SELECT * FROM tickets WHERE guild_id=? AND user_id=?', (gid, uid))
        return rows[0] if rows else None

    async def start(self, i, kind):
        async with self.bot.member_lock(i.guild_id, i.user.id):
            row = await self.state(i.guild_id, i.user.id)
            if row and row['status'] == 'open':
                return await reply(i, f"У тебя уже открыт тикет: <#{row['channel_id']}>.")
            if not row or row['status'] == 'closed':
                await self.bot.db.execute('INSERT OR REPLACE INTO tickets(guild_id,user_id,kind,updated_at) VALUES(?,?,?,?)',
                                          (i.guild_id, i.user.id, kind, time.time()))
            elif row['kind'] != kind:
                # Switching unfinished form is explicit via category button.
                await self.bot.db.execute("UPDATE tickets SET kind=?,answers='[]',channel_id=0,updated_at=? WHERE guild_id=? AND user_id=?",
                                          (kind, time.time(), i.guild_id, i.user.id))
        row = await self.state(i.guild_id, i.user.id)
        offset = len(json.loads(row['answers']))
        if offset < len(questions(kind)):
            await i.response.send_modal(Form(self, kind, offset))
        else:
            await self.create(i)

    async def create(self, i):
        if not i.response.is_done():
            await i.response.defer(ephemeral=True)
        async with self.bot.member_lock(i.guild_id, i.user.id):
            row = await self.state(i.guild_id, i.user.id)
            if not row or row['status'] != 'draft':
                return await reply(i, 'Тикет уже отправлен или форма устарела.')
            answers = json.loads(row['answers'])
            if len(answers) != len(questions(row['kind'])):
                return await reply(i, 'Сначала заполни анкету полностью.')
            cfg = self.settings(i.guild_id)
            category = i.guild.get_channel(cfg['category_id'])
            server = server_for(row['kind'], answers)
            ids = reviewer_roles(cfg, row['kind'], server)
            roles = [i.guild.get_role(rid) for rid in ids]
            if not isinstance(category, discord.CategoryChannel) or not roles or any(r is None or r.is_default() for r in roles):
                return await reply(i, 'Не найдена категория или одна из ролей направления. Тикет не создан; проверьте настройки.')
            access = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True)
            overwrites = {i.guild.default_role: discord.PermissionOverwrite(view_channel=False),
                          i.user: access,
                          i.guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True)}
            overwrites.update({role: access for role in roles})
            marker = f'ticket:{i.guild_id}:{i.user.id}:{row["updated_at"]}'
            channel = next((c for c in category.text_channels if c.topic == marker), None) if row['channel_id'] == 0 else i.guild.get_channel(row['channel_id'])
            if channel is None:
                channel = await category.create_text_channel(f'ticket-{row["kind"]}-{i.user.id}', overwrites=overwrites, topic=marker)
            await self.bot.db.execute('UPDATE tickets SET channel_id=? WHERE guild_id=? AND user_id=?', (channel.id,i.guild_id,i.user.id))
            embeds = []
            for offset in range(0,len(answers),5):
                e = discord.Embed(title=KINDS[row['kind']], description=f'Автор: {i.user.mention} · Сервер: {server.upper()}', color=0xF2B66D)
                for q, a in zip(questions(row['kind'])[offset:offset+5], answers[offset:offset+5]):
                    e.add_field(name=q, value=discord.utils.escape_markdown(a)[:1024], inline=False)
                embeds.append(e)
            for e in embeds:
                await channel.send(embed=e, allowed_mentions=discord.AllowedMentions.none())
            await channel.send('🎫 Обращение приехало! Команда скоро заглянет.', view=Controls(self), allowed_mentions=discord.AllowedMentions.none())
            await self.bot.db.execute("UPDATE tickets SET status='open' WHERE guild_id=? AND user_id=?", (i.guild_id,i.user.id))
            await reply(i, f'Готово! Твой тикет: {channel.mention}')

    @app_commands.command(name='tickets_panel', description='Опубликовать или обновить панель тикетов')
    @staff_check()
    async def publish(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        cfg = self.settings(i.guild_id)
        if not cfg:
            return await reply(i, 'Тикеты не настроены в конфигурации.')
        async with self.bot.member_lock(i.guild_id, cfg['panel_channel_id']):
            channel = i.guild.get_channel(cfg['panel_channel_id'])
            if not isinstance(channel, discord.TextChannel):
                return await reply(i, 'Не найден текстовый канал для панели.')
            e = discord.Embed(title='🎫 Тикетная ТОРТИКА — давай разберёмся',
                              description='Есть жалоба, хочешь обжаловать наказание или попасть в команду? Выбирай кнопку ниже.\n\n'+RECRUIT,
                              color=0xF2B66D)
            rows = await self.bot.db.rows('SELECT message_id FROM ticket_panels WHERE guild_id=?', (i.guild_id,))
            message = None
            if rows:
                try:
                    message = await channel.fetch_message(rows[0]['message_id'])
                except discord.NotFound:
                    pass
            if message:
                await message.edit(embed=e,view=Panel(self))
            else:
                message = await channel.send(embed=e,view=Panel(self))
                await self.bot.db.execute('INSERT OR REPLACE INTO ticket_panels VALUES(?,?)',(i.guild_id,message.id))
            await reply(i, f'Панель готова: {message.jump_url}')
