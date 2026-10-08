import asyncio
import json
from pathlib import Path

import discord
from discord.ext import commands

from services.rules import AntiProfanitySystem, is_staff, level_for_xp, normalize


class ActivityLevelSystem:
    def __init__(self, bot):
        self.bot = bot

    async def process(self, message):
        cfg = self.bot.cfg(message.guild.id)["xp"]
        text = normalize(message.content)
        role_ids = {r.id for r in message.author.roles}
        if (len(text) < cfg["min_message_length"] or text.startswith(tuple(cfg["command_prefixes"]))
                or message.channel.id in cfg["ignored_channels"] or role_ids & set(cfg["ignored_roles"])):
            return
        # Settle before fetching: deleted/native-AutoMod-blocked messages never earn initial XP.
        await asyncio.sleep(cfg["settlement_seconds"])
        try:
            current = await message.channel.fetch_message(message.id)
        except discord.HTTPException:
            return
        if current.content != message.content:
            return
        async with self.bot.member_lock(message.guild.id, message.author.id):
            old, new, amount = await self.bot.db.award_xp(message.guild.id, message.author.id, message.id, text, cfg)
            if amount and level_for_xp(old) != level_for_xp(new):
                await self.bot.roles.apply(message.author, verified=True)


class Automod(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.activity = ActivityLevelSystem(bot)
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
        if await self.moderate(message):
            return
        await self.activity.process(message)

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload):
        if not self.bot.operational or not payload.guild_id or not self.bot.cfg(payload.guild_id):
            return
        channel = self.bot.get_channel(payload.channel_id)
        if not channel or "content" not in payload.data:
            return
        try:
            message = await channel.fetch_message(payload.message_id)
        except discord.HTTPException:
            return
        if not message.author.bot and isinstance(message.author, discord.Member):
            await self.moderate(message)

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
