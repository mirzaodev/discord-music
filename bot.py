import asyncio
import discord
from discord.ext import commands
from discord import app_commands

import config
import database as db


intents = discord.Intents.default()
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    db.init_db()
    await bot.tree.sync()
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("Slash commands synced globally.")


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
