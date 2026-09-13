from __future__ import annotations
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv
from .storage import Store

load_dotenv()
store = Store(Path(__file__).resolve().parents[2] / "data" / "truth_or_dare.sqlite3")

def admin(i: discord.Interaction) -> bool:
    return bool(i.user.guild_permissions and i.user.guild_permissions.manage_guild)

class TruthDareBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.none())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        if guild_id := os.getenv("GUILD_ID"):
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        self.daily_post.start()

    async def send_prompt(self, channel, mode="mixed", theme=None, intensity=None):
        prompt = store.choose_prompt(mode, theme, intensity)
        if not prompt: return False
        embed = discord.Embed(title=f"{prompt.kind.title()} • Level {prompt.intensity}", description=prompt.text, color=discord.Color.blurple())
        if prompt.theme: embed.set_footer(text=f"Theme: {prompt.theme}")
        await channel.send(embed=embed)
        store.record_post(prompt.id)
        return True

    @tasks.loop(minutes=1)
    async def daily_post(self):
        channel_id = os.getenv("CHANNEL_ID")
        if store.paused() or not channel_id: return
        timezone = store.get_setting("timezone", os.getenv("TIMEZONE", "America/Chicago"))
        now = datetime.now(ZoneInfo(timezone))
        if now.strftime("%H:%M") != store.get_setting("post_time", os.getenv("POST_TIME", "19:00")): return
        day = now.strftime("%Y-%m-%d")
        if store.get_setting("last_post_date") == day: return
        channel = self.get_channel(int(channel_id))
        if channel and await self.send_prompt(channel): store.set_setting("last_post_date", day)

bot = TruthDareBot()

@bot.tree.command(name="ask-now", description="Post a Truth or Dare prompt now.")
async def ask_now(i: discord.Interaction, mode: str = "mixed", theme: str | None = None, intensity: app_commands.Range[int, 1, 5] | None = None):
    if mode not in {"truth", "dare", "mixed"}:
        await i.response.send_message("Mode must be truth, dare, or mixed.", ephemeral=True); return
    await i.response.defer(ephemeral=True)
    try:
        posted = await bot.send_prompt(i.channel, mode, theme, intensity)
    except (discord.Forbidden, discord.HTTPException):
        await i.followup.send("I could not post in this channel. Check my View Channel, Send Messages, and Embed Links permissions.", ephemeral=True)
        return
    if not posted:
        await i.followup.send("No prompt matches those filters.", ephemeral=True)
        return
    await i.followup.send("Prompt posted.", ephemeral=True)

@bot.tree.command(name="pause", description="Pause scheduled posts (admin only).")
async def pause(i: discord.Interaction):
    if not admin(i): await i.response.send_message("Admin only.", ephemeral=True); return
    store.set_setting("paused", "true"); await i.response.send_message("Scheduled posts paused.", ephemeral=True)

@bot.tree.command(name="resume", description="Resume scheduled posts (admin only).")
async def resume(i: discord.Interaction):
    if not admin(i): await i.response.send_message("Admin only.", ephemeral=True); return
    store.set_setting("paused", "false"); await i.response.send_message("Scheduled posts resumed.", ephemeral=True)

@bot.tree.command(name="schedule", description="Set daily time and timezone (admin only).")
async def schedule(i: discord.Interaction, time: str, timezone: str = "America/Chicago"):
    if not admin(i): await i.response.send_message("Admin only.", ephemeral=True); return
    try: datetime.strptime(time, "%H:%M"); ZoneInfo(timezone)
    except ValueError: await i.response.send_message("Use HH:MM and a valid IANA timezone.", ephemeral=True); return
    store.set_setting("post_time", time); store.set_setting("timezone", timezone)
    await i.response.send_message(f"Daily post set for {time} ({timezone}).", ephemeral=True)

@bot.tree.command(name="settings", description="Show bot settings.")
async def settings(i: discord.Interaction):
    await i.response.send_message(f"Paused: {store.paused()}\\nTime: {store.get_setting('post_time', '19:00')}\\nTimezone: {store.get_setting('timezone', 'America/Chicago')}", ephemeral=True)

@bot.tree.command(name="add-prompt", description="Add one prompt (admin only).")
async def add_prompt(i: discord.Interaction, kind: str, intensity: app_commands.Range[int, 1, 5], text: str, theme: str = ""):
    if not admin(i): await i.response.send_message("Admin only.", ephemeral=True); return
    try: added = store.add_prompts([(kind, intensity, theme or None, text)])
    except ValueError as error: await i.response.send_message(str(error), ephemeral=True); return
    await i.response.send_message("Prompt added." if added else "That prompt already exists.", ephemeral=True)

@bot.tree.command(name="import-prompts", description="Import: type|intensity|theme|text (admin only).")
async def import_prompts(i: discord.Interaction, prompts: str):
    if not admin(i): await i.response.send_message("Admin only.", ephemeral=True); return
    try:
        rows = [tuple(part.strip() for part in line.split("|", 3)) for line in prompts.splitlines() if line.strip()]
        added = store.add_prompts([(kind, int(level), theme or None, text) for kind, level, theme, text in rows])
    except (ValueError, TypeError):
        await i.response.send_message("Each line needs: type|intensity|theme|prompt text", ephemeral=True); return
    await i.response.send_message(f"Imported {added} prompt(s).", ephemeral=True)

def main():
    auth = os.getenv("BOT_AUTH")
    if not auth: raise SystemExit("BOT_AUTH is missing from the local environment.")
    bot.run(auth)

if __name__ == "__main__": main()