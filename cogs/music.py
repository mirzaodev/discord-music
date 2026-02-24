import asyncio
import discord
from discord import app_commands
from discord.ext import commands

from queue_manager import queue_manager, SongEntry
from ytdl_source import YTDLSource


def _fmt_duration(seconds: int) -> str:
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _ensure_voice(self, interaction: discord.Interaction):
        """Return the caller's VoiceChannel or send an ephemeral error."""
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message(
                "You need to be in a voice channel first.", ephemeral=True
            )
            return None
        return interaction.user.voice.channel

    async def _get_voice_client(
        self,
        interaction: discord.Interaction,
        channel: discord.VoiceChannel,
    ) -> discord.VoiceClient:
        """Join the channel if needed, or reuse/move an existing voice client."""
        gq = queue_manager.get(interaction.guild_id)
        vc = interaction.guild.voice_client
        if vc is None:
            vc = await channel.connect()
        elif vc.channel != channel:
            await vc.move_to(channel)
        gq.voice_client = vc
        return vc

    async def _play_next(self, guild_id: int) -> None:
        """
        Dequeue the next song and start playback.
        Called from the after= callback (worker thread context via
        run_coroutine_threadsafe) and directly when nothing is playing.
        """
        gq = queue_manager.get(guild_id)
        async with gq._lock:
            entry = gq.pop_next()
            if entry is None:
                gq.current = None
                return
            gq.current = entry

        # Voice client may have disconnected
        if not gq.voice_client or not gq.voice_client.is_connected():
            gq.current = None
            return

        try:
            source = await YTDLSource.from_query(entry.url, loop=self.bot.loop)
        except Exception as exc:
            print(f"[audio error] Could not resolve '{entry.title}': {exc}")
            # skip to next song
            await self._play_next(guild_id)
            return

        def after_playing(error):
            if error:
                print(f"[playback error] {error}")
            fut = asyncio.run_coroutine_threadsafe(
                self._play_next(guild_id), self.bot.loop
            )
            try:
                fut.result()
            except Exception as exc:
                print(f"[after callback error] {exc}")

        gq.voice_client.play(source, after=after_playing)

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    @app_commands.command(name="play", description="Play a song or add it to the queue")
    @app_commands.describe(query="Song name or URL")
    async def play(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer(thinking=True)

        channel = await self._ensure_voice(interaction)
        if channel is None:
            return

        try:
            meta = await YTDLSource.fetch_metadata_only(query, loop=self.bot.loop)
        except ValueError as exc:
            await interaction.followup.send(str(exc))
            return

        vc = await self._get_voice_client(interaction, channel)
        gq = queue_manager.get(interaction.guild_id)

        entry = SongEntry(
            title=meta["title"],
            url=meta["url"],
            duration=meta["duration"],
            requester=interaction.user,
            thumbnail=meta.get("thumbnail"),
        )

        gq.add(entry)

        if vc.is_playing() or vc.is_paused():
            embed = discord.Embed(
                description=f"Added to queue (#{len(gq)}): **{entry.title}**",
                color=discord.Color.blurple(),
            )
            embed.add_field(name="Duration", value=_fmt_duration(entry.duration))
            await interaction.followup.send(embed=embed)
        else:
            await self._play_next(interaction.guild_id)
            embed = discord.Embed(
                title="Now Playing",
                description=f"**{entry.title}**",
                color=discord.Color.green(),
            )
            embed.add_field(name="Duration", value=_fmt_duration(entry.duration))
            embed.add_field(name="Requested by", value=interaction.user.mention)
            if entry.thumbnail:
                embed.set_thumbnail(url=entry.thumbnail)
            await interaction.followup.send(embed=embed)

    @app_commands.command(
        name="playnext",
        description="Insert a song right after the current one",
    )
    @app_commands.describe(query="Song name or URL")
    async def playnext(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer(thinking=True)

        channel = await self._ensure_voice(interaction)
        if channel is None:
            return

        try:
            meta = await YTDLSource.fetch_metadata_only(query, loop=self.bot.loop)
        except ValueError as exc:
            await interaction.followup.send(str(exc))
            return

        vc = await self._get_voice_client(interaction, channel)
        gq = queue_manager.get(interaction.guild_id)

        entry = SongEntry(
            title=meta["title"],
            url=meta["url"],
            duration=meta["duration"],
            requester=interaction.user,
            thumbnail=meta.get("thumbnail"),
        )

        if vc.is_playing() or vc.is_paused():
            gq.add_next(entry)
            embed = discord.Embed(
                description=f"Playing next: **{entry.title}**",
                color=discord.Color.blurple(),
            )
            embed.add_field(name="Duration", value=_fmt_duration(entry.duration))
            await interaction.followup.send(embed=embed)
        else:
            # Nothing playing — just start it
            gq.add(entry)
            await self._play_next(interaction.guild_id)
            embed = discord.Embed(
                title="Now Playing",
                description=f"**{entry.title}**",
                color=discord.Color.green(),
            )
            embed.add_field(name="Duration", value=_fmt_duration(entry.duration))
            embed.add_field(name="Requested by", value=interaction.user.mention)
            if entry.thumbnail:
                embed.set_thumbnail(url=entry.thumbnail)
            await interaction.followup.send(embed=embed)

    @app_commands.command(name="skip", description="Skip the current song")
    async def skip(self, interaction: discord.Interaction):
        gq = queue_manager.get(interaction.guild_id)
        vc = interaction.guild.voice_client
        if not vc or not (vc.is_playing() or vc.is_paused()):
            await interaction.response.send_message(
                "Nothing is playing right now.", ephemeral=True
            )
            return
        title = gq.current.title if gq.current else "Unknown"
        gq.skip()
        await interaction.response.send_message(f"Skipped: **{title}**")

    @app_commands.command(name="stop", description="Stop playback and clear the queue")
    async def stop(self, interaction: discord.Interaction):
        gq = queue_manager.get(interaction.guild_id)
        gq.clear()
        vc = interaction.guild.voice_client
        if vc:
            vc.stop()
            await vc.disconnect()
            gq.voice_client = None
        await interaction.response.send_message("Stopped and cleared the queue.")

    @app_commands.command(name="pause", description="Pause playback")
    async def pause(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc and vc.is_playing():
            vc.pause()
            await interaction.response.send_message("Paused.")
        else:
            await interaction.response.send_message(
                "Nothing is playing.", ephemeral=True
            )

    @app_commands.command(name="resume", description="Resume playback")
    async def resume(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc and vc.is_paused():
            vc.resume()
            await interaction.response.send_message("Resumed.")
        else:
            await interaction.response.send_message(
                "Playback is not paused.", ephemeral=True
            )

    @app_commands.command(name="nowplaying", description="Show the currently playing song")
    async def nowplaying(self, interaction: discord.Interaction):
        gq = queue_manager.get(interaction.guild_id)
        if not gq.current:
            await interaction.response.send_message(
                "Nothing is playing right now.", ephemeral=True
            )
            return
        song = gq.current
        embed = discord.Embed(
            title="Now Playing",
            description=f"**{song.title}**",
            color=discord.Color.green(),
        )
        embed.add_field(name="Duration", value=_fmt_duration(song.duration))
        embed.add_field(name="Requested by", value=song.requester.mention)
        if song.thumbnail:
            embed.set_thumbnail(url=song.thumbnail)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="queue", description="Show the current queue")
    async def queue_cmd(self, interaction: discord.Interaction):
        gq = queue_manager.get(interaction.guild_id)
        entries = gq.list_entries()

        if not entries and not gq.current:
            await interaction.response.send_message(
                "The queue is empty.", ephemeral=True
            )
            return

        lines = []
        for i, e in enumerate(entries[:20]):
            lines.append(
                f"`{i + 1}.` **{e.title}** [{_fmt_duration(e.duration)}]"
                f" — {e.requester.display_name}"
            )
        if len(entries) > 20:
            lines.append(f"*... and {len(entries) - 20} more*")

        embed = discord.Embed(
            title=f"Queue — {len(entries)} song(s) up next",
            description="\n".join(lines) if lines else "*Queue is empty*",
            color=discord.Color.blue(),
        )
        if gq.current:
            embed.set_footer(text=f"Now playing: {gq.current.title}")
        await interaction.response.send_message(embed=embed)


    @app_commands.command(name="shuffle", description="Shuffle the songs waiting in the queue")
    async def shuffle(self, interaction: discord.Interaction):
        gq = queue_manager.get(interaction.guild_id)
        if len(gq) < 2:
            await interaction.response.send_message(
                "Need at least 2 songs in the queue to shuffle.", ephemeral=True
            )
            return
        gq.shuffle()
        await interaction.response.send_message(
            f"Shuffled {len(gq)} songs in the queue."
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
