"""Read-only live check: connect briefly, inspect the configured channel, send nothing."""
import asyncio
import json
import os
from pathlib import Path
import discord
from dotenv import load_dotenv
from truth_or_dare_bot.config import Config

async def check():
    load_dotenv(Path(__file__).resolve().parents[1] / '.env')
    config = Config.from_env()
    intents = discord.Intents.none()
    intents.guilds = True
    client = discord.Client(intents=intents)
    result = {}

    @client.event
    async def on_ready():
        try:
            channel = client.get_channel(config.channel_id) or await client.fetch_channel(config.channel_id)
            correct_guild = channel.guild.id == config.guild_id
            permissions = channel.permissions_for(channel.guild.me)
            result.update(connected=True, correct_guild=correct_guild,
                          view_channel=permissions.view_channel,
                          send_messages=permissions.send_messages,
                          embed_links=permissions.embed_links)
        except Exception as error:
            result.update(error_type=type(error).__name__)
        finally:
            await client.close()
    try:
        await asyncio.wait_for(client.start(os.environ['BOT_AUTH']), timeout=45)
    except Exception as error:
        result.update(error_type=type(error).__name__)
    finally:
        await client.close()
    print(json.dumps(result))
    if not all(result.get(k) for k in ('connected','correct_guild','view_channel','send_messages','embed_links')):
        raise SystemExit(1)

if __name__ == '__main__': asyncio.run(check())
