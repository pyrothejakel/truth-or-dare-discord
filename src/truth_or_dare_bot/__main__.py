from __future__ import annotations
import asyncio
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo
import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv
from .config import Config, clock_time, timezone_name, in_quiet_hours
from .storage import Store, PROMPT_KINDS

log = logging.getLogger(__name__)

class BotTree(app_commands.CommandTree):
    async def interaction_check(self, i):
        if i.guild_id != self.client.config.guild_id or i.channel_id != self.client.config.channel_id:
            await i.response.send_message("Use this bot in its configured server channel.", ephemeral=True)
            return False
        return True

    async def on_error(self, i, error):
        # Exception types are useful; raw exceptions may contain configuration or content.
        log.error("Command failed: %s", type(error).__name__)
        message = "That action could not be confirmed. Check /settings before retrying."
        if i.response.is_done(): await i.followup.send(message, ephemeral=True)
        else: await i.response.send_message(message, ephemeral=True)

class TruthDareBot(discord.Client):
    def __init__(self, config, store):
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.config, self.store = config, store
        self.tree = BotTree(self)
        self.post_lock = asyncio.Lock()
        self.register_commands()

    async def setup_hook(self):
        guild = discord.Object(id=self.config.guild_id)
        self.tree.copy_global_to(guild=guild)
        synced = await self.tree.sync(guild=guild)
        log.info("Registered %d server commands", len(synced))
        self.daily_post.start()

    async def on_ready(self):
        channel = await self.target_channel()
        permissions = channel.permissions_for(channel.guild.me)
        if not (permissions.view_channel and permissions.send_messages and permissions.embed_links):
            log.error("Configured channel is missing View Channel, Send Messages or Embed Links")
        else:
            log.info("READY: Discord connected; configured channel and posting permissions verified; prompts=%d", self.store.count_prompts())

    async def target_channel(self):
        channel = self.get_channel(self.config.channel_id) or await self.fetch_channel(self.config.channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)) or channel.guild.id != self.config.guild_id:
            raise ValueError("Configured channel must be a text channel in the configured server.")
        return channel

    def local_now(self):
        return datetime.now(ZoneInfo(self.store.get_setting("timezone", self.config.timezone)))

    def quiet(self, now):
        return in_quiet_hours(now, self.store.get_setting("quiet_start", ""), self.store.get_setting("quiet_end", ""))

    async def send_prompt(self, channel, mode="mixed", theme=None, intensity=None, day=None):
        async with self.post_lock:
            if self.quiet(self.local_now()): return "Quiet hours are active."
            if day and self.store.paused(): return "Scheduled posts are paused."
            prompt = self.store.choose_prompt(mode, theme, intensity)
            if prompt is None: return "No prompt matches those filters."
            if day and not self.store.claim_day(day): return "Today's scheduled post was already attempted."
            embed = discord.Embed(title=f"{PROMPT_KINDS[prompt.kind]} • Level {prompt.intensity}", description=prompt.text, color=discord.Color.blurple())
            embed.set_footer(text=f"Theme: {prompt.theme or 'any'} • Participation is optional; you can pass.")
            # Record only after Discord confirms delivery. Never retry an ambiguous send.
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            self.store.record_post(prompt.id)
            if day: self.store.complete_day(day)
            return "Prompt posted."

    async def scheduled_tick(self, now=None):
        now = now or self.local_now()
        if self.store.paused() or self.quiet(now): return
        # Catch a restart or brief outage after the scheduled minute, once per local day.
        if now.strftime("%H:%M") < self.store.get_setting("post_time", self.config.post_time): return
        day = now.date().isoformat()
        if self.store.get_setting("last_post_date") == day: return
        if self.store.execute("SELECT 1 FROM scheduled_days WHERE day=?", (day,)).fetchone(): return
        channel = await self.target_channel()
        level = self.store.get_setting("intensity", "")
        result = await self.send_prompt(channel, self.store.get_setting("mode", "mixed"),
                                        self.store.get_setting("theme", "") or None,
                                        int(level) if level else None, day=day)
        log.info("Daily scheduler: %s", result)

    @tasks.loop(seconds=30)
    async def daily_post(self):
        try:
            await self.scheduled_tick()
        except (discord.HTTPException, OSError) as error:
            log.warning("Scheduled post could not be confirmed: %s", type(error).__name__)
        except Exception as error:
            log.error("Scheduler stopped after storage/configuration failure: %s", type(error).__name__)
            asyncio.create_task(self.close())

    @daily_post.before_loop
    async def wait_for_discord(self):
        await self.wait_until_ready()

    async def close(self):
        self.daily_post.cancel()
        await super().close()

    async def require_admin(self, i):
        if not isinstance(i.user, discord.Member) or not i.user.guild_permissions.manage_guild:
            await i.response.send_message("Manage Server permission is required.", ephemeral=True)
            return False
        return True

    async def theme_suggestions(self, i, current):
        # Autocomplete callbacks need their own boundary check before reading data.
        if i.guild_id != self.config.guild_id or i.channel_id != self.config.channel_id:
            return []
        try:
            return [app_commands.Choice(name=theme, value=theme)
                    for theme in self.store.list_themes(current)]
        except Exception as error:
            log.warning("Theme suggestions unavailable: %s", type(error).__name__)
            return []

    def register_commands(self):
        @self.tree.command(name="ask-now", description="Post a Truth, Dare or Never Have I Ever prompt. Leave filters blank for Mixed.")
        @app_commands.describe(
            mode="Choose Truth, Dare, NHIE (Never Have I Ever), or Mixed (all three). Leave blank for Mixed.",
            theme="Pick a saved theme, or type to search (e.g. fun). Leave blank to include all themes.",
            intensity="Pick an exact level from 1 to 5, as assigned to each prompt. Leave blank to include all levels.",
        )
        @app_commands.choices(
            mode=[app_commands.Choice(name="Mixed - Truth, Dare or NHIE (default)", value="mixed"),
                  app_commands.Choice(name="Truth - questions", value="truth"),
                  app_commands.Choice(name="Dare - challenges", value="dare"),
                  app_commands.Choice(name="NHIE - Never Have I Ever", value="nhie")],
            intensity=[app_commands.Choice(name=f"Level {level}", value=level) for level in range(1, 6)],
        )
        async def ask_now(i: discord.Interaction, mode: str = "mixed", theme: str | None = None,
                          intensity: app_commands.Range[int, 1, 5] | None = None):
            if mode not in {*PROMPT_KINDS, "mixed"}:
                await i.response.send_message("Mode must be truth, dare, nhie, or mixed.", ephemeral=True)
                return
            await i.response.defer(ephemeral=True)
            try:
                result = await self.send_prompt(await self.target_channel(), mode, theme, intensity)
            except discord.HTTPException:
                result = "Posting could not be confirmed. Check the channel and my posting permissions before retrying."
            await i.followup.send(result, ephemeral=True)

        @ask_now.autocomplete("theme")
        async def ask_now_theme(i: discord.Interaction, current: str):
            return await self.theme_suggestions(i, current)

        @self.tree.command(name="pause", description="Pause scheduled posts (Manage Server required).")
        @app_commands.default_permissions(manage_guild=True)
        async def pause(i: discord.Interaction):
            if not await self.require_admin(i): return
            self.store.set_setting("paused", "true")
            await i.response.send_message("Scheduled posts paused. /ask-now remains available outside quiet hours.", ephemeral=True)

        @self.tree.command(name="resume", description="Resume scheduled posts (Manage Server required).")
        @app_commands.default_permissions(manage_guild=True)
        async def resume(i: discord.Interaction):
            if not await self.require_admin(i): return
            self.store.set_setting("paused", "false")
            await i.response.send_message("Scheduled posts resumed. If today's time has passed, a post may follow shortly.", ephemeral=True)

        @self.tree.command(name="schedule", description="Set daily local time and timezone (Manage Server required).")
        @app_commands.default_permissions(manage_guild=True)
        async def schedule(i: discord.Interaction, time: str, timezone: str = "America/Chicago"):
            if not await self.require_admin(i): return
            try: clock_time(time); timezone_name(timezone)
            except ValueError as error:
                await i.response.send_message(str(error), ephemeral=True); return
            self.store.set_settings({"post_time": time, "timezone": timezone})
            await i.response.send_message(f"Daily post set for {time} ({timezone}).", ephemeral=True)

        @self.tree.command(name="quiet-hours", description="Set quiet hours, or disable them (Manage Server required).")
        @app_commands.default_permissions(manage_guild=True)
        async def quiet_hours(i: discord.Interaction, start: str = "", end: str = ""):
            if not await self.require_admin(i): return
            try:
                if start or end: clock_time(start); clock_time(end)
                if start and start == end: raise ValueError("Start and end must differ. Leave both empty to disable.")
            except ValueError as error:
                await i.response.send_message(str(error), ephemeral=True); return
            self.store.set_settings({"quiet_start": start, "quiet_end": end})
            await i.response.send_message(f"Quiet hours: {start}–{end}." if start else "Quiet hours disabled.", ephemeral=True)

        @self.tree.command(name="configure", description="Set daily mode, theme and intensity (Manage Server required).")
        @app_commands.default_permissions(manage_guild=True)
        @app_commands.describe(mode="Choose Truth, Dare, NHIE (Never Have I Ever), or Mixed (all three). Leave blank for Mixed.")
        @app_commands.choices(mode=[
            app_commands.Choice(name="Mixed - Truth, Dare or NHIE (default)", value="mixed"),
            app_commands.Choice(name="Truth - questions", value="truth"),
            app_commands.Choice(name="Dare - challenges", value="dare"),
            app_commands.Choice(name="NHIE - Never Have I Ever", value="nhie"),
        ])
        async def configure(i: discord.Interaction, mode: str = "mixed", theme: str = "",
                            intensity: app_commands.Range[int, 1, 5] | None = None):
            if not await self.require_admin(i): return
            try:
                if len(theme) > 80: raise ValueError("Theme must be at most 80 characters.")
                if self.store.choose_prompt(mode, theme or None, intensity) is None:
                    raise ValueError("No prompts match. Add matching prompts before choosing these settings.")
            except ValueError as error:
                await i.response.send_message(str(error), ephemeral=True); return
            self.store.set_settings({"mode": mode, "theme": theme.strip(), "intensity": intensity or ""})
            await i.response.send_message("Daily prompt settings saved.", ephemeral=True)

        @self.tree.command(name="settings", description="Show schedule, quiet hours, filters and prompt count.")
        async def settings(i: discord.Interaction):
            s = self.store
            lines = [f"Paused: {s.paused()}", f"Daily: {s.get_setting('post_time', self.config.post_time)} ({s.get_setting('timezone', self.config.timezone)})",
                     f"Mode: {PROMPT_KINDS.get(s.get_setting('mode', 'mixed'), s.get_setting('mode', 'mixed'))}", f"Theme: {s.get_setting('theme', '') or 'any'}",
                     f"Intensity: {s.get_setting('intensity', '') or 'any'}",
                     f"Quiet hours: {s.get_setting('quiet_start', '') or 'disabled'} – {s.get_setting('quiet_end', '')}",
                     f"Prompts: {s.count_prompts()}", f"Last confirmed scheduled date: {s.get_setting('last_post_date', 'none')}"]
            await i.response.send_message("\n".join(lines), ephemeral=True)

        @self.tree.command(name="add-prompt", description="Add one prompt (Manage Server required).")
        @app_commands.default_permissions(manage_guild=True)
        @app_commands.describe(
            kind="Choose Truth, Dare or NHIE (Never Have I Ever).",
            text='Enter the whole prompt. For NHIE, start with "Never have I ever...".',
        )
        @app_commands.choices(kind=[
            app_commands.Choice(name="Truth - questions", value="truth"),
            app_commands.Choice(name="Dare - challenges", value="dare"),
            app_commands.Choice(name="NHIE - Never Have I Ever", value="nhie"),
        ])
        async def add_prompt(i: discord.Interaction, kind: str, intensity: app_commands.Range[int, 1, 5], text: str, theme: str = ""):
            if not await self.require_admin(i): return
            try: added = self.store.add_prompts([(kind, intensity, theme or None, text)])
            except ValueError as error:
                await i.response.send_message(str(error), ephemeral=True); return
            await i.response.send_message("Prompt added." if added else "That prompt already exists.", ephemeral=True)

        @self.tree.command(name="import-prompts", description="Import type|intensity|theme|text lines (Manage Server required).")
        @app_commands.default_permissions(manage_guild=True)
        @app_commands.describe(prompts="One line per prompt: truth/dare/nhie|1-5|theme|full prompt text. Theme may be blank.")
        async def import_prompts(i: discord.Interaction, prompts: str):
            if not await self.require_admin(i): return
            try:
                rows = [tuple(p.strip() for p in line.split("|", 3)) for line in prompts.splitlines() if line.strip()]
                if not rows: raise ValueError("No prompts supplied.")
                added = self.store.add_prompts(rows)
            except (ValueError, TypeError):
                await i.response.send_message("Nothing imported. Each line needs truth/dare/nhie|1-5|theme|prompt text. Theme max 80; text max 3500 characters.", ephemeral=True); return
            await i.response.send_message(f"Imported {added} prompt(s).", ephemeral=True)

def main():
    load_dotenv()
    auth = os.getenv("BOT_AUTH")
    if not auth: raise SystemExit("BOT_AUTH is missing. Enter it privately in the runtime environment.")
    try:
        config = Config.from_env()
        store = Store(config.database)
    except ValueError as error:
        raise SystemExit(str(error)) from None
    except Exception as error:
        raise SystemExit(f"Database initialization failed ({type(error).__name__}). Check the private connection configuration.") from None
    try:
        bot = TruthDareBot(config, store)
        bot.run(auth, root_logger=True)
    finally:
        store.close()

if __name__ == "__main__": main()
