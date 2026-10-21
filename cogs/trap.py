"""Explicitly labelled honeypot; only live messages after the warning is published."""
import asyncio
import logging
import random
import time

import discord
from discord.ext import commands

LOG = logging.getLogger("security.trap")
NAMES = ("tregdfkwasdaw-dwadsawdsaaw", "чат-с-котиками", "не-основной-чат",
         "мемредакция-бан", "не-пиши-сюда", "бэнбинбанбум", "бумбумбум", "хыхыхыхы")


def warning(count):
    return ("# ⛔ НЕ ПИШИТЕ НИЧЕГО В ДАННЫЙ КАНАЛ — ЭТО ЛОВУШКА ДЛЯ БОТОВ!\n\n"
            "Если вы что-то сюда напишете, вы получите **автоматический БАН**. "
            "Это относится и к картинкам, стикерам и сообщениям в ветках.\n\n"
            f"**Забанено: {count}**\n\n"
            "Если вас заставили сюда написать — **НЕ ПИШИТЕ**.\n"
            "Если вы заставите кого-либо сюда написать, даже ради шутки, — вам будет выдан **БАН**. "
            "Такие случаи разбирает администрация.\n\n"
            "Название канала меняется каждый час. Предупреждение действует при любом названии.")


def is_trap(channel, channel_id):
    return channel.id == channel_id or getattr(channel, "parent_id", None) == channel_id


class Trap(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.armed = {}
        self.locks = {}

    async def state(self, gid):
        await self.bot.db.execute("CREATE TABLE IF NOT EXISTS trap_state (guild_id INTEGER PRIMARY KEY, "
                                  "message_id INTEGER NOT NULL DEFAULT 0, renamed_at REAL NOT NULL DEFAULT 0)")
        await self.bot.db.execute("INSERT OR IGNORE INTO trap_state(guild_id) VALUES(?)", (gid,))
        return (await self.bot.db.rows("SELECT * FROM trap_state WHERE guild_id=?", (gid,)))[0]

    async def count(self, gid):
        return (await self.bot.db.rows("SELECT COUNT(*) AS n FROM moderation_actions "
                                      "WHERE guild_id=? AND action='trap_ban'", (gid,)))[0]["n"]

    async def refresh(self, guild, cfg):
        channel = guild.get_channel(cfg["channel_id"])
        if not isinstance(channel, discord.TextChannel):
            self.armed.pop(guild.id, None)
            return
        perms = channel.permissions_for(guild.me)
        if not (guild.me.guild_permissions.ban_members and perms.view_channel and perms.send_messages
                and perms.read_message_history and perms.manage_channels):
            self.armed.pop(guild.id, None)
            LOG.error("trap_permissions_missing guild=%s", guild.id)
            return
        state = await self.state(guild.id)
        content = warning(await self.count(guild.id))
        panel = None
        if state["message_id"]:
            try:
                panel = await channel.fetch_message(state["message_id"])
            except discord.NotFound:
                pass
        if panel is None:
            self.armed.pop(guild.id, None)
            panel = await channel.send(content, allowed_mentions=discord.AllowedMentions.none())
            await self.bot.db.execute("UPDATE trap_state SET message_id=? WHERE guild_id=?", (panel.id, guild.id))
        elif panel.content != content:
            await panel.edit(content=content, allowed_mentions=discord.AllowedMentions.none())
        if not panel.pinned and perms.manage_messages:
            await panel.pin(reason="Предупреждение о канале-ловушке")
        topic = "⛔ НЕ ПИШИТЕ: автоматический БАН за любое сообщение. Ловушка для ботов."
        if channel.topic != topic:
            await channel.edit(topic=topic, reason="Явное предупреждение о ловушке")
        self.armed.setdefault(guild.id, time.time())
        if time.time() - state["renamed_at"] >= 3600:
            await channel.edit(name=random.choice([n for n in NAMES if n != channel.name]),
                               reason="Почасовое имя ловушки")
            await self.bot.db.execute("UPDATE trap_state SET renamed_at=? WHERE guild_id=?", (time.time(), guild.id))

    async def run(self):
        while not self.bot.is_closed():
            for gid, g in self.bot.config["guilds"].items():
                cfg = g.get("trap", {})
                guild = self.bot.get_guild(gid)
                if not guild or not cfg.get("enabled"):
                    continue
                try:
                    async with self.locks.setdefault(gid, asyncio.Lock()):
                        await self.refresh(guild, cfg)
                except Exception:
                    self.armed.pop(gid, None)
                    LOG.exception("trap_refresh_failed guild=%s", gid)
            await asyncio.sleep(60)

    @commands.Cog.listener()
    async def on_message(self, message):
        if not self.bot.operational or not message.guild:
            return
        gid = message.guild.id
        cfg = (self.bot.cfg(gid) or {}).get("trap", {})
        if not cfg.get("enabled") or not is_trap(message.channel, cfg["channel_id"]):
            return
        if (message.author.id == self.bot.user.id or message.webhook_id or message.is_system()
                or not isinstance(message.author, discord.Member)):
            return
        async with self.locks.setdefault(gid, asyncio.Lock()):
            if gid not in self.armed or message.created_at.timestamp() < self.armed[gid]:
                return
            member = message.author
            if member.id == message.guild.owner_id or member.top_role >= message.guild.me.top_role:
                await self.bot.audit(gid, member.id, 0, "trap_ban_failed", "Discord role hierarchy")
                return
            try:
                await message.guild.ban(member, reason=f"Канал-ловушка: сообщение {message.id}", delete_message_seconds=0)
            except discord.HTTPException as exc:
                await self.bot.audit(gid, member.id, 0, "trap_ban_failed", type(exc).__name__)
                return
            await self.bot.audit(gid, member.id, 0, "trap_ban", f"message={message.id}")
