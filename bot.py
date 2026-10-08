"""Security Agent. --check is completely offline; --preflight is read-only Discord inspection."""
import argparse
import asyncio
import logging
import os
import signal
import tempfile
import time
import weakref
from collections import defaultdict
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

from cogs.automod import Automod
from cogs.common import embed, reply
from cogs.levels import Levels
from cogs.moderation import Moderation
from cogs.verification import Verification
from services.config import load_config
from services.database import Database
from services.logging_setup import configure_logging
from services.moderation_service import ModerationService
from services.role_sync import RoleSync
from services.startup import startup_check
from services.messages import actor_label, participant_label, event_title, event_detail

LOG = logging.getLogger("security")


class SecurityTree(app_commands.CommandTree):
    async def interaction_check(self, interaction):
        if not self.client.operational:
            await reply(interaction, "Дежурный ещё открывает КПП. Дай мне немного времени и повтори команду.")
            return False
        if not interaction.guild_id or not self.client.cfg(interaction.guild_id):
            await reply(interaction, "Этот пульт работает только на сервере, для которого настроен дежурный.")
            return False
        return True

    async def on_error(self, interaction, error):
        cause = getattr(error, "original", error)
        if isinstance(cause, app_commands.CheckFailure):
            text = str(cause)
        else:
            text = "Упс, команда споткнулась. Дежурные, проверьте мои права и /modlog."
        await self.client.audit(interaction.guild_id or 0, interaction.user.id, interaction.user.id,
                                "command_error", type(cause).__name__)
        await reply(interaction, text, color=0xED4245)


class SecurityBot(commands.Bot):
    def __init__(self, config, *, preflight=False):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.members = True
        intents.guild_messages = True
        intents.dm_messages = True
        intents.message_content = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, tree_cls=SecurityTree,
                         allowed_mentions=discord.AllowedMentions.none(), help_command=None)
        self.config = config
        self.preflight = preflight
        self.db = Database(config["database"])
        self.roles = RoleSync(self)
        self.moderation = ModerationService(self)
        self.verification = Verification(self)
        self._locks = weakref.WeakValueDictionary()
        self.repair_locks = defaultdict(asyncio.Lock)
        self.last_repair = {}
        self.background = set()
        self.ready_once = False
        self.started_at = time.time()
        self.operational = False
        self.preflight_errors = 0

    def cfg(self, guild_id):
        return self.config["guilds"].get(guild_id)

    def member_lock(self, guild_id, user_id):
        key = (guild_id, user_id)
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.background.add(task)
        def done(completed):
            self.background.discard(completed)
            if not completed.cancelled() and completed.exception():
                LOG.error("background_error %s", type(completed.exception()).__name__)
        task.add_done_callback(done)
        return task

    async def audit(self, guild_id, user_id, actor_id, action, detail=""):
        await self.db.audit(guild_id, user_id, actor_id, action, detail)
        LOG.info("%s guild=%s user=%s actor=%s", action, guild_id, user_id, actor_id)

    async def setup_local(self):
        await self.db.open()
        await self.add_cog(self.verification)
        await self.add_cog(Moderation(self))
        await self.add_cog(Levels(self))
        await self.add_cog(Automod(self))
        for command in self.tree.get_commands():
            command.guild_only = True
        self.verification.register_views()

    async def setup_hook(self):
        # Preflight loads no listeners, commands, views or mutating repair jobs.
        if self.preflight:
            await self.db.open()
        else:
            await self.setup_local()

    async def on_ready(self):
        if self.ready_once:
            return
        self.ready_once = True
        for guild_id in self.config["guilds"]:
            guild = self.get_guild(guild_id)
            if guild is None:
                LOG.error("Startup check guild=%s UNAVAILABLE", guild_id)
                self.preflight_errors += 1
                continue
            _, errors = await startup_check(self, guild)
            self.preflight_errors += errors
        if self.preflight:
            await self.close()
            return
        # Startup errors block startup entirely: do not mutate an unprepared server.
        if self.preflight_errors:
            LOG.error("Startup blocked: correct config, roles and permissions; run --preflight again")
            await self.close()
            return
        for guild_id in self.config["guilds"]:
            guild = self.get_guild(guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        self.maintenance.start()
        self.deliver_logs.start()
        self.operational = True
        LOG.info("SecurityAgent ready")

    async def repair_guild(self, guild, *, force=False):
        if self.repair_locks[guild.id].locked():
            return
        cfg = self.cfg(guild.id)
        if not force and time.time()-self.last_repair.get(guild.id, 0) < cfg["levels"]["repair_interval_seconds"]:
            return
        async with self.repair_locks[guild.id]:
            scan_at = time.time()
            # Member chunks let us catch joins missed while the gateway was offline.
            if not guild.chunked:
                async with asyncio.timeout(45):
                    await guild.chunk(cache=True)
            checkpoint = await self.db.rows("SELECT scanned_at FROM guild_state WHERE guild_id=?", (guild.id,))
            since = checkpoint[0]["scanned_at"] if checkpoint else self.started_at
            for member in guild.members:
                if not member.bot and member.joined_at and member.joined_at.timestamp() >= since:
                    state = await self.db.state(guild.id, member.id)
                    if not state:
                        await self.verification.on_member_join(member)
                        await asyncio.sleep(cfg["levels"]["sync_delay_seconds"])
            await self.db.execute("INSERT OR REPLACE INTO guild_state VALUES(?,?)", (guild.id, scan_at))
            rows = await self.db.rows("SELECT user_id FROM verification WHERE guild_id=?", (guild.id,))
            errors, processed = 0, 0
            for row in rows:
                if guild.id in self.verification.bulk_guilds:
                    if force:
                        await self.audit(guild.id, 0, 0, "role_repair_paused", "Manual verification requested")
                    else:
                        LOG.info("role_repair_paused guild=%s", guild.id)
                    return
                uid = row["user_id"]
                try:
                    async with asyncio.timeout(45):
                        member = guild.get_member(uid) or await guild.fetch_member(uid)
                        if member.bot:
                            continue
                        async with self.member_lock(guild.id, uid):
                            state = await self.db.state(guild.id, uid)
                            if state["status"] == "passed":
                                await self.verification.complete(member)
                            elif state["status"] == "verified":
                                if not await self.roles.apply(member, verified=True):
                                    errors += 1
                            else:
                                # Renew near expiry, and repair roles even while timeout is still active.
                                current = member.timed_out_until.timestamp() if member.timed_out_until else 0
                                if current < time.time()+86400:
                                    await self.verification.restrict(member)
                                else:
                                    await self.roles.apply(member, verified=False)
                                if not state["last_sent"]:
                                    await self.verification.deliver(member)
                            processed += 1
                except discord.NotFound:
                    continue
                except Exception as exc:
                    errors += 1
                    await self.audit(guild.id, uid, 0, "repair_error", type(exc).__name__)
                finally:
                    await asyncio.sleep(cfg["levels"]["sync_delay_seconds"])
            self.last_repair[guild.id] = time.time()
            if force or errors:
                await self.audit(guild.id, 0, 0, "role_repair_completed", f"processed={processed}; errors={errors}")
            else:
                # Healthy periodic checks belong in host logs, not the Discord moderation channel.
                LOG.info("role_repair_completed guild=%s processed=%s errors=0", guild.id, processed)

    @tasks.loop(seconds=60)
    async def maintenance(self):
        for gid in self.config["guilds"]:
            guild = self.get_guild(gid)
            if guild:
                try:
                    await self.repair_guild(guild)
                except Exception as exc:
                    LOG.error("maintenance_error guild=%s type=%s", gid, type(exc).__name__)
        # Retain credit receipts for 30 days for deletion rollback; old zero-credit spam events are pruned too.
        await self.db.execute("DELETE FROM xp_events WHERE created_at<?", (time.time()-30*86400,))
        await self.db.execute("DELETE FROM deleted_messages WHERE created_at<?", (time.time()-30*86400,))

    @tasks.loop(seconds=5)
    async def deliver_logs(self):
        for gid, cfg in self.config["guilds"].items():
            guild = self.get_guild(gid)
            channel = guild.get_channel(cfg["modlog_channel_id"]) if guild else None
            if channel is None:
                continue
            rows = await self.db.rows("SELECT * FROM moderation_actions WHERE guild_id=? AND delivered=0 "
                                       "ORDER BY id LIMIT 10", (gid,))
            if not rows:
                continue
            try:
                batch = embed("Что нового на КПП. Подробности для дежурных — в /modlog.", title="Журнал дежурного • ТОРТИК")
                for row in rows:
                    batch.add_field(name=f"#{row['id']} {event_title(row['action'])}",
                                    value=f"Кого касается: {participant_label(row['user_id'])} · "
                                          f"Кто: {actor_label(row['moderator_id'])}\n"
                                          + discord.utils.escape_markdown(event_detail(row['action'], row['detail']))[:350],
                                    inline=False)
                async with asyncio.timeout(30):
                    await channel.send(embed=batch)
                for row in rows:
                    await self.db.execute("UPDATE moderation_actions SET delivered=1 WHERE guild_id=? AND id=?",
                                          (gid, row["id"]))
            except (discord.HTTPException, TimeoutError) as exc:
                LOG.warning("modlog_delivery_error guild=%s type=%s", gid, type(exc).__name__)

    async def on_error(self, event, *args, **kwargs):
        # Event payloads include private messages. Never serialize args or exception text.
        LOG.error("event_error event=%s", event)
        if event == "on_ready":
            self.preflight_errors += 1
            await self.close()

    async def close(self):
        self.operational = False
        self.maintenance.cancel()
        self.deliver_logs.cancel()
        running = list(self.background)
        running += [t for t in (self.maintenance.get_task(), self.deliver_logs.get_task()) if t]
        for task in running:
            task.cancel()
        if running:
            await asyncio.gather(*running, return_exceptions=True)
        await super().close()
        await self.db.close()


async def offline_check(cfg):
    with tempfile.TemporaryDirectory() as directory:
        cfg = {**cfg, "database": str(Path(directory)/"check.db")}
        async with SecurityBot(cfg) as bot:
            await bot.setup_local()
            serialized = [cmd.to_dict(bot.tree) for cmd in bot.tree.get_commands()]
            assert serialized and bot.persistent_views
            print(f"OFFLINE OK: SQLite schema, config, {len(serialized)} top-level commands, persistent views")
            if 0 in cfg["guilds"]:
                print("CONFIG TEMPLATE: set guild ID and channel IDs before --preflight/production")


async def main():
    parser = argparse.ArgumentParser()
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true", help="Offline config/database/command validation; no token")
    modes.add_argument("--preflight", action="store_true", help="Read-only Discord role/permission inspection; token required")
    args = parser.parse_args()
    load_dotenv()
    token = os.getenv("DISCORD_TOKEN", "").strip()
    configure_logging("INFO", token)
    try:
        try:
            cfg = load_config(offline=args.check)
        except (ValueError, KeyError, TypeError) as exc:
            LOG.error("config_invalid: %s", str(exc))
            return 2
        configure_logging(cfg["logging_level"], token)
        if args.check:
            await offline_check(cfg)
            return 0
        if not token:
            LOG.error("DISCORD_TOKEN is missing; set it in .env")
            return 2
        async with SecurityBot(cfg, preflight=args.preflight) as bot:
            loop = asyncio.get_running_loop()
            if os.name != "nt":
                loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.create_task(bot.close()))
            await bot.start(token)
            return 2 if bot.preflight_errors else 0
    except Exception as exc:
        # No raw exception message: may include authentication/HTTP data.
        LOG.error("startup_failed type=%s; check config, token presence and permissions", type(exc).__name__)
        return 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
