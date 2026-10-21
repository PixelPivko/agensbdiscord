import asyncio
import json
import time
from pathlib import Path

import discord
from discord.ext import commands

from services.rules import AntiProfanitySystem, is_staff, level_for_xp, normalize


class MessageReader:
    """Shared request pacing for XP settlement and uncached message edits."""

    def __init__(self):
        self.lock = asyncio.Lock()
        self.next_read = 0.0
        self.interval = 1.0

    async def read(self, channel, message_id):
        try:
            async with asyncio.timeout(30):
                async with self.lock:
                    await asyncio.sleep(max(0, self.next_read - time.monotonic()))
                    try:
                        return await channel.fetch_message(message_id)
                    finally:
                        self.next_read = time.monotonic() + self.interval
        except (discord.HTTPException, TimeoutError):
            return None


class ActivityLevelSystem:
    def __init__(self, bot, reader=None):
        self.bot = bot
        self.reader = reader or MessageReader()

    async def process(self, message):
        cfg = self.bot.cfg(message.guild.id)["xp"]
        text = normalize(message.content)
        role_ids = {r.id for r in message.author.roles}
        if (len(text) < cfg["min_message_length"] or text.startswith(tuple(cfg["command_prefixes"]))
                or message.channel.id in cfg["ignored_channels"] or role_ids & set(cfg["ignored_roles"])):
            return
        # Settle before fetching: deleted/native-AutoMod-blocked messages never earn initial XP.
        await asyncio.sleep(cfg["settlement_seconds"])
        current = await self.reader.read(message.channel, message.id)
        if current is None or current.content != message.content:
            return
        async with self.bot.member_lock(message.guild.id, message.author.id):
            old, new, amount = await self.bot.db.award_xp(message.guild.id, message.author.id, message.id, text, cfg)
            if amount and level_for_xp(old) != level_for_xp(new):
                await self.bot.roles.apply(message.author, verified=True)


class Automod(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.reader = MessageReader()
        self.activity = ActivityLevelSystem(bot, self.reader)
        self.pending_edits = set()
        self.filters = {}
        for gid, cfg in bot.config["guilds"].items():
            p = cfg["profanity"]
            self.filters[gid] = AntiProfanitySystem(json.loads(Path(p["blacklist"]).read_text(encoding="utf-8")),
                                                  json.loads(Path(p["whitelist"]).read_text(encoding="utf-8")))

    async def moderate(self, message):
        cfg = self.bot.cfg(message.guild.id)
        p = cfg["profanity"]
        if (message.channel.id in p["ignored_channels"] or {r.id for r in message.author.roles} & set(p["ignored_roles"])
                or (p["staff_immune"] and is_staff(message.author, cfg))):
            return False
        if not self.filters[message.guild.id].matches(message.content):
            return False
        try:
            await message.delete()
            await self.bot.audit(message.guild.id, message.author.id, 0, "profanity_deleted", f"message={message.id}")
        except discord.NotFound:
            pass
        except discord.HTTPException as exc:
            await self.bot.audit(message.guild.id, message.author.id, 0, "bot_permission_error",
                                 f"profanity delete: {type(exc).__name__}")
        async with self.bot.member_lock(message.guild.id, message.author.id):
            await self.bot.db.revoke_xp(message.guild.id, message.id)
            warning_id, count = await self.bot.moderation.warn(message.author, message.guild.me,
                                                             "Запрещённая лексика", "antiprofanity", message.id)
            state = await self.bot.db.state(message.guild.id, message.author.id)
            if state and state["status"] == "verified":
                await self.bot.roles.apply(message.author, verified=True)
        if warning_id:
            try:
                await message.author.send("Псс, дежурный ТОРТИК на связи. Давай без запрещённых слов — "
                                          "хочется, чтобы в чате было уютно всем. "
                                          f"Записал предупреждение #{warning_id} за лексику. "
                                          f"Активных предупреждений: {count}. При повторениях возможны ограничения по правилам сервера.")
            except discord.HTTPException:
                pass
        return True

    @commands.Cog.listener()
    async def on_message(self, message):
        if not self.bot.operational or not message.guild or not self.bot.cfg(message.guild.id) or message.author.bot or not isinstance(message.author, discord.Member):
            return
        trap = self.bot.cfg(message.guild.id).get("trap", {})
        if trap.get("enabled") and (message.channel.id == trap["channel_id"] or getattr(message.channel, "parent_id", None) == trap["channel_id"]):
            return
        if await self.moderate(message):
            return
        await self.activity.process(message)

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload):
        if not self.bot.operational or not payload.guild_id or not self.bot.cfg(payload.guild_id):
            return
        content = payload.data.get("content")
        if not isinstance(content, str):
            return
        # Gateway updates for bot messages/embeds must not trigger GETs of our own modlog.
        author = payload.data.get("author") or {}
        cached = getattr(payload, "cached_message", None)
        if author.get("bot") or payload.data.get("webhook_id") or (cached and cached.author.bot):
            return
        cfg = self.bot.cfg(payload.guild_id)["profanity"]
        if payload.channel_id in cfg["ignored_channels"]:
            return
        # Text is already supplied by Discord. Clean text needs no API read at all.
        if not self.filters[payload.guild_id].matches(content):
            return
        channel = self.bot.get_channel(payload.channel_id)
        key = (payload.guild_id, payload.message_id)
        if channel is None or key in self.pending_edits:
            return
        self.pending_edits.add(key)
        try:
            message = await self.reader.read(channel, payload.message_id)
            if message is not None and not message.author.bot and isinstance(message.author, discord.Member):
                # Recheck the fetched, latest text; the edit may have already been corrected.
                await self.moderate(message)
        finally:
            self.pending_edits.discard(key)

    async def revoke(self, guild_id, message_id):
        uid = await self.bot.db.revoke_xp(guild_id, message_id)
        if uid:
            guild = self.bot.get_guild(guild_id)
            member = guild.get_member(uid) if guild else None
            if member:
                async with self.bot.member_lock(guild_id, uid):
                    state = await self.bot.db.state(guild_id, uid)
                    if state and state["status"] == "verified":
                        await self.bot.roles.apply(member, verified=True)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload):
        if self.bot.operational and payload.guild_id and self.bot.cfg(payload.guild_id):
            await self.revoke(payload.guild_id, payload.message_id)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload):
        if self.bot.operational and payload.guild_id and self.bot.cfg(payload.guild_id):
            for message_id in payload.message_ids:
                await self.revoke(payload.guild_id, message_id)
