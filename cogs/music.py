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


def _build_now_playing_embed(
    song: SongEntry,
    guild_id: int,
    *,
    paused: bool = False,
) -> discord.Embed:
    """Build a rich 'Now Playing' embed with song info and next-up."""
    status = "\u23F8\uFE0F  Paused" if paused else "\u25B6\uFE0F  Now Playing"
    embed = discord.Embed(
        title=status,
        description=f"**{song.title}**",
        color=discord.Color.from_rgb(30, 215, 96),  # Spotify-ish green
    )
    embed.add_field(name="Duration", value=f"`{_fmt_duration(song.duration)}`", inline=True)
    embed.add_field(name="Requested by", value=song.requester.mention, inline=True)

    # Next song preview
    gq = queue_manager.get(guild_id)
    entries = gq.list_entries()
    if entries:
        nxt = entries[0]
        embed.add_field(
            name="Up Next",
            value=f"{nxt.title}  `{_fmt_duration(nxt.duration)}`",
            inline=False,
        )
    else:
        embed.add_field(name="Up Next", value="Nothing — queue is empty", inline=False)

    if song.thumbnail:
        embed.set_thumbnail(url=song.thumbnail)

    embed.set_footer(text=f"{len(entries)} song(s) in queue")
    return embed


class NowPlayingView(discord.ui.View):
    """Persistent buttons attached to the now-playing panel."""

    def __init__(self, cog: "Music", guild_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = guild_id

    @discord.ui.button(label="Pause", style=discord.ButtonStyle.secondary, emoji="\u23F8\uFE0F")
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if not vc:
            await interaction.response.send_message("Not connected.", ephemeral=True)
            return

        gq = queue_manager.get(self.guild_id)
        if vc.is_playing():
            vc.pause()
            button.label = "Resume"
            button.emoji = "\u25B6\uFE0F"
            embed = _build_now_playing_embed(gq.current, self.guild_id, paused=True)
            await interaction.response.edit_message(embed=embed, view=self)
        elif vc.is_paused():
            vc.resume()
            button.label = "Pause"
            button.emoji = "\u23F8\uFE0F"
            embed = _build_now_playing_embed(gq.current, self.guild_id, paused=False)
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.primary, emoji="\u23ED\uFE0F")
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        gq = queue_manager.get(self.guild_id)
        vc = interaction.guild.voice_client
        if not vc or not (vc.is_playing() or vc.is_paused()):
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        title = gq.current.title if gq.current else "Unknown"
        gq.skip()
        await interaction.response.send_message(f"Skipped: **{title}**")


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
        gq.text_channel = interaction.channel
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
        await self._send_now_playing(guild_id)

    async def _send_now_playing(self, guild_id: int) -> None:
        """Send (or replace) the now-playing panel in the text channel."""
        gq = queue_manager.get(guild_id)
        if not gq.current or not gq.text_channel:
            return

        # Delete the previous panel so chat stays clean
        if gq.now_playing_message:
            try:
                await gq.now_playing_message.delete()
            except discord.HTTPException:
                pass

        embed = _build_now_playing_embed(gq.current, guild_id)
        view = NowPlayingView(self, guild_id)
        gq.now_playing_message = await gq.text_channel.send(embed=embed, view=view)

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    @app_commands.command(name="play", description="Play a song or add it to the queue")
    @app_commands.describe(query="Song name, URL, or playlist URL")
    async def play(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer(thinking=True)

        channel = await self._ensure_voice(interaction)
        if channel is None:
            return

        # Detect playlist URLs (SoundCloud /sets/, YouTube /playlist, etc.)
        is_playlist = "/sets/" in query or "playlist" in query

        if is_playlist:
            try:
                tracks = await YTDLSource.fetch_playlist_metadata(
                    query, loop=self.bot.loop
                )
            except ValueError as exc:
                await interaction.followup.send(str(exc))
                return

            if not tracks:
                await interaction.followup.send("No playable tracks found in that playlist.")
                return

            vc = await self._get_voice_client(interaction, channel)
            gq = queue_manager.get(interaction.guild_id)
            was_playing = vc.is_playing() or vc.is_paused()

            for t in tracks:
                gq.add(SongEntry(
                    title=t["title"],
                    url=t["url"],
                    duration=t["duration"],
                    requester=interaction.user,
                    thumbnail=t.get("thumbnail"),
                ))

            if not was_playing:
                await self._play_next(interaction.guild_id)

            embed = discord.Embed(
                description=(
                    f"{'Added' if was_playing else 'Started playing'} "
                    f"**{len(tracks)}** track(s) from playlist."
                ),
                color=discord.Color.green(),
            )
            await interaction.followup.send(embed=embed)
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
            await interaction.followup.send(
                f"Started playing **{entry.title}**", silent=True
            )

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
            await interaction.followup.send(
                f"Started playing **{entry.title}**", silent=True
            )

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
        # Clean up the now-playing panel
        if gq.now_playing_message:
            try:
                await gq.now_playing_message.delete()
            except discord.HTTPException:
                pass
            gq.now_playing_message = None
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
            gq = queue_manager.get(interaction.guild_id)
            if gq.now_playing_message and gq.current:
                embed = _build_now_playing_embed(gq.current, interaction.guild_id, paused=True)
                try:
                    await gq.now_playing_message.edit(embed=embed)
                except discord.HTTPException:
                    pass
            await interaction.response.send_message("Paused.", ephemeral=True)
        else:
            await interaction.response.send_message(
                "Nothing is playing.", ephemeral=True
            )

    @app_commands.command(name="resume", description="Resume playback")
    async def resume(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc and vc.is_paused():
            vc.resume()
            gq = queue_manager.get(interaction.guild_id)
            if gq.now_playing_message and gq.current:
                embed = _build_now_playing_embed(gq.current, interaction.guild_id, paused=False)
                try:
                    await gq.now_playing_message.edit(embed=embed)
                except discord.HTTPException:
                    pass
            await interaction.response.send_message("Resumed.", ephemeral=True)
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
        vc = interaction.guild.voice_client
        paused = vc.is_paused() if vc else False
        embed = _build_now_playing_embed(gq.current, interaction.guild_id, paused=paused)
        view = NowPlayingView(self, interaction.guild_id)
        gq.text_channel = interaction.channel
        await interaction.response.send_message(embed=embed, view=view)

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
