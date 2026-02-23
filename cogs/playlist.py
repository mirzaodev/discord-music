import sqlite3
import discord
from discord import app_commands
from discord.ext import commands

import database as db
from queue_manager import queue_manager, SongEntry
from ytdl_source import YTDLSource


def _fmt_duration(seconds: int) -> str:
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


class PlaylistCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    playlist_group = app_commands.Group(
        name="playlist",
        description="Manage saved playlists",
    )

    # ------------------------------------------------------------------
    # /playlist create
    # ------------------------------------------------------------------

    @playlist_group.command(name="create", description="Create a new playlist")
    @app_commands.describe(name="Name for the playlist")
    async def create(self, interaction: discord.Interaction, name: str):
        try:
            db.create_playlist(str(interaction.guild_id), name)
            await interaction.response.send_message(
                f"Playlist **{name}** created successfully."
            )
        except sqlite3.IntegrityError:
            await interaction.response.send_message(
                f"A playlist named **{name}** already exists.", ephemeral=True
            )

    # ------------------------------------------------------------------
    # /playlist add
    # ------------------------------------------------------------------

    @playlist_group.command(name="add", description="Add a song to a playlist")
    @app_commands.describe(name="Playlist name", song="Song name or YouTube URL")
    async def add(self, interaction: discord.Interaction, name: str, song: str):
        await interaction.response.defer(thinking=True)

        row = db.get_playlist(str(interaction.guild_id), name)
        if row is None:
            await interaction.followup.send(
                f"Playlist **{name}** not found.", ephemeral=True
            )
            return

        try:
            meta = await YTDLSource.fetch_metadata_only(song, loop=self.bot.loop)
        except ValueError as exc:
            await interaction.followup.send(str(exc))
            return

        db.add_song_to_playlist(
            row["id"], meta["title"], meta["url"], meta["duration"]
        )
        await interaction.followup.send(
            f"Added **{meta['title']}** to playlist **{name}**."
        )

    # ------------------------------------------------------------------
    # /playlist remove
    # ------------------------------------------------------------------

    @playlist_group.command(
        name="remove", description="Remove a song from a playlist by its number"
    )
    @app_commands.describe(
        name="Playlist name",
        index="Song number (as shown in /playlist view)",
    )
    async def remove(self, interaction: discord.Interaction, name: str, index: int):
        row = db.get_playlist(str(interaction.guild_id), name)
        if row is None:
            await interaction.response.send_message(
                f"Playlist **{name}** not found.", ephemeral=True
            )
            return

        removed = db.remove_song_from_playlist(row["id"], index - 1)  # 1-based → 0-based
        if removed:
            await interaction.response.send_message(
                f"Removed song #{index} from **{name}**."
            )
        else:
            await interaction.response.send_message(
                f"No song at index {index} in **{name}**.", ephemeral=True
            )

    # ------------------------------------------------------------------
    # /playlist list
    # ------------------------------------------------------------------

    @playlist_group.command(name="list", description="List all playlists for this server")
    async def list_cmd(self, interaction: discord.Interaction):
        rows = db.list_playlists(str(interaction.guild_id))
        if not rows:
            await interaction.response.send_message(
                "No playlists yet. Create one with `/playlist create`.", ephemeral=True
            )
            return

        lines = [f"`{r['name']}` — {r['song_count']} song(s)" for r in rows]
        embed = discord.Embed(
            title="Server Playlists",
            description="\n".join(lines),
            color=discord.Color.purple(),
        )
        embed.set_footer(text=f"{len(rows)} playlist(s) total")
        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------------
    # /playlist view
    # ------------------------------------------------------------------

    @playlist_group.command(name="view", description="View the songs in a playlist")
    @app_commands.describe(name="Playlist name")
    async def view(self, interaction: discord.Interaction, name: str):
        row = db.get_playlist(str(interaction.guild_id), name)
        if row is None:
            await interaction.response.send_message(
                f"Playlist **{name}** not found.", ephemeral=True
            )
            return

        songs = db.get_playlist_songs(row["id"])
        if not songs:
            await interaction.response.send_message(
                f"Playlist **{name}** is empty. Add songs with `/playlist add`.",
                ephemeral=True,
            )
            return

        lines = []
        for s in songs[:25]:
            dur = _fmt_duration(s["duration"]) if s["duration"] else "?"
            lines.append(f"`{s['position'] + 1}.` **{s['title']}** [{dur}]")
        if len(songs) > 25:
            lines.append(f"*... and {len(songs) - 25} more*")

        embed = discord.Embed(
            title=f"Playlist: {name}",
            description="\n".join(lines),
            color=discord.Color.orange(),
        )
        embed.set_footer(text=f"{len(songs)} song(s)")
        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------------
    # /playlist play
    # ------------------------------------------------------------------

    @playlist_group.command(name="play", description="Enqueue all songs from a playlist")
    @app_commands.describe(name="Playlist name")
    async def play_playlist(self, interaction: discord.Interaction, name: str):
        await interaction.response.defer(thinking=True)

        # Caller must be in a voice channel
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.followup.send(
                "You need to be in a voice channel first.", ephemeral=True
            )
            return

        row = db.get_playlist(str(interaction.guild_id), name)
        if row is None:
            await interaction.followup.send(
                f"Playlist **{name}** not found."
            )
            return

        songs = db.get_playlist_songs(row["id"])
        if not songs:
            await interaction.followup.send(
                f"Playlist **{name}** is empty."
            )
            return

        # Join / reuse voice client
        channel = interaction.user.voice.channel
        gq = queue_manager.get(interaction.guild_id)
        vc = interaction.guild.voice_client
        if vc is None:
            vc = await channel.connect()
        elif vc.channel != channel:
            await vc.move_to(channel)
        gq.voice_client = vc

        # Enqueue all songs
        for s in songs:
            gq.add(
                SongEntry(
                    title=s["title"],
                    url=s["url"],
                    duration=s["duration"] or 0,
                    requester=interaction.user,
                )
            )

        was_playing = vc.is_playing() or vc.is_paused()

        # Start playback if idle
        if not was_playing:
            # Import here to avoid circular import at module level
            from cogs.music import Music
            music_cog: Music = interaction.client.cogs.get("Music")
            if music_cog:
                await music_cog._play_next(interaction.guild_id)

        embed = discord.Embed(
            title=f"Playlist: {name}",
            description=(
                f"{'Added' if was_playing else 'Started playing'} "
                f"**{len(songs)}** song(s) from **{name}**."
            ),
            color=discord.Color.green(),
        )
        await interaction.followup.send(embed=embed)

    # ------------------------------------------------------------------
    # /playlist delete
    # ------------------------------------------------------------------

    @playlist_group.command(name="delete", description="Delete a playlist entirely")
    @app_commands.describe(name="Playlist name")
    async def delete(self, interaction: discord.Interaction, name: str):
        deleted = db.delete_playlist(str(interaction.guild_id), name)
        if deleted:
            await interaction.response.send_message(
                f"Deleted playlist **{name}**."
            )
        else:
            await interaction.response.send_message(
                f"Playlist **{name}** not found.", ephemeral=True
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(PlaylistCog(bot))
