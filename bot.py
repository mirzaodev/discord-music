import asyncio
import discord
from discord.ext import commands
from discord import app_commands

import config
import database as db
import cache_manager


intents = discord.Intents.default()
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    db.init_db()
    cache_manager.ensure_cache_dir()
    if config.GUILD_ID:
        guild = discord.Object(id=config.GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
        print(f"Slash commands synced to guild {config.GUILD_ID} (instant).")
    else:
        await bot.tree.sync()
        print("Slash commands synced globally (up to 1 hour to propagate).")
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
):
    msg = "An error occurred."
    if isinstance(error, app_commands.CommandInvokeError):
        inner = error.original
        if isinstance(inner, ValueError):
            msg = str(inner)
        else:
            print(f"[ERROR] {type(inner).__name__}: {inner}")
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


async def main():
    async with bot:
        await bot.load_extension("cogs.music")
        await bot.load_extension("cogs.playlist")
        await bot.start(config.DISCORD_TOKEN)


asyncio.run(main())
