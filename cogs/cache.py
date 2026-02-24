import asyncio
import os

import discord
from discord import app_commands
from discord.ext import commands

import cache_manager
import database as db
from queue_manager import queue_manager, SongEntry
from ytdl_source import YTDLSource, _download_single_track, _extract_playlist, LOCAL_FFMPEG_OPTIONS


CACHE_PLAYLIST_DIR = cache_manager.CACHE_DIR / "playlists"


def _ensure_playlist_cache_dir():
    CACHE_PLAYLIST_DIR.mkdir(parents=True, exist_ok=True)


def _fmt_duration(seconds: int) -> str:
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


class CacheCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="cache",
        description="Cache a SoundCloud playlist locally (download + encode to opus)",
    )
    @app_commands.describe(
        url="SoundCloud playlist URL (/sets/ link)",
        name="Name for the cached playlist",
    )
    async def cache_playlist(self, interaction: discord.Interaction, url: str, name: str):
        # Validate it's a SoundCloud playlist
        if "soundcloud.com" not in url or "/sets/" not in url:
            await interaction.response.send_message(
                "Only SoundCloud playlist URLs (containing `/sets/`) are supported.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)
        _ensure_playlist_cache_dir()

        guild_id = str(interaction.guild_id)

        # Fetch playlist metadata
        try:
            data = await self.bot.loop.run_in_executor(
                None, lambda: _extract_playlist(url)
            )
        except Exception as exc:
            await interaction.followup.send(f"Failed to fetch playlist: {exc}")
            return

        entries = [e for e in (data.get("entries") or []) if e is not None]
        if not entries:
            await interaction.followup.send("No playable tracks found in that playlist.")
            return

        # Create or get the cached playlist record
        playlist_id = db.create_cached_playlist(guild_id, name, url)
        already_cached = db.get_cached_playlist_urls(playlist_id)
        next_pos = db.get_next_cached_track_position(playlist_id)

        # Filter to only new tracks
        new_entries = []
        for entry in entries:
            track_url = entry.get("webpage_url") or entry.get("url")
            if track_url and track_url not in already_cached:
                new_entries.append(entry)

        if not new_entries:
            await interaction.followup.send(
                f"Playlist **{name}** is already fully cached ({len(already_cached)} tracks)."
            )
            return

        # Progress message
        progress_msg = await interaction.followup.send(
            f"Caching **{len(new_entries)}** new track(s) from **{name}** "
            f"({len(already_cached)} already cached)...",
            wait=True,
        )

        # Download each new track
        cached_count = 0
        failed_count = 0
        playlist_dir = CACHE_PLAYLIST_DIR / f"{guild_id}_{playlist_id}"
        playlist_dir.mkdir(parents=True, exist_ok=True)

        for i, entry in enumerate(new_entries):
            track_url = entry.get("webpage_url") or entry.get("url")
            title = entry.get("title", "Unknown")
            duration = int(entry.get("duration") or 0)

            # Deterministic filename from URL
            file_hash = cache_manager.url_to_hash(track_url)
            output_path = str(playlist_dir / f"{file_hash}.opus")

            # Skip if file already exists on disk (e.g. partial re-run)
            if os.path.isfile(output_path):
                db.add_cached_playlist_track(
                    playlist_id, next_pos, title, track_url, duration, output_path
                )
                next_pos += 1
                cached_count += 1
                continue

            try:
                result = await self.bot.loop.run_in_executor(
                    None, lambda u=track_url, o=output_path: _download_single_track(u, o)
                )
                actual_path = result.get("_downloaded_file", output_path)
                if os.path.isfile(actual_path):
                    db.add_cached_playlist_track(
                        playlist_id, next_pos, title, track_url, duration, actual_path
                    )
                    next_pos += 1
                    cached_count += 1
                else:
                    failed_count += 1
            except Exception as exc:
                print(f"[cache] Failed to download '{title}': {exc}")
                failed_count += 1

            # Update progress every 5 tracks
            if (i + 1) % 5 == 0:
                try:
                    await progress_msg.edit(
                        content=(
                            f"Caching **{name}**... {cached_count}/{len(new_entries)} done"
                            f"{f', {failed_count} failed' if failed_count else ''}"
                        )
                    )
                except discord.HTTPException:
                    pass

        # Final status
        total = len(already_cached) + cached_count
        result_lines = [f"Cached **{name}**: **{total}** total track(s)."]
        if cached_count:
            result_lines.append(f"  New: {cached_count}")
        if already_cached:
            result_lines.append(f"  Previously cached: {len(already_cached)}")
        if failed_count:
            result_lines.append(f"  Failed: {failed_count}")

        embed = discord.Embed(
            title=f"Cache Complete: {name}",
            description="\n".join(result_lines),
            color=discord.Color.green() if not failed_count else discord.Color.yellow(),
        )
        try:
            await progress_msg.edit(content=None, embed=embed)
        except discord.HTTPException:
            await interaction.followup.send(embed=embed)

    @app_commands.command(
        name="playlocal",
        description="Play a cached playlist directly from local files (no encoding delay)",
    )
    @app_commands.describe(name="Name of the cached playlist")
    async def play_local(self, interaction: discord.Interaction, name: str):
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message(
                "You need to be in a voice channel first.", ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True)

        guild_id = str(interaction.guild_id)
        playlist = db.get_cached_playlist(guild_id, name)
        if playlist is None:
            await interaction.followup.send(
                f"No cached playlist named **{name}**. Use `/cache` first.",
                ephemeral=True,
            )
            return

        tracks = db.get_cached_playlist_tracks(playlist["id"])
        if not tracks:
            await interaction.followup.send(
                f"Cached playlist **{name}** has no tracks.", ephemeral=True
            )
            return

        # Filter to tracks whose files still exist on disk
        valid_tracks = [t for t in tracks if os.path.isfile(t["file_path"])]
        if not valid_tracks:
            await interaction.followup.send(
                f"All cached files for **{name}** are missing. Re-run `/cache` to re-download.",
                ephemeral=True,
            )
            return

        # Join voice
        channel = interaction.user.voice.channel
        gq = queue_manager.get(interaction.guild_id)
        vc = interaction.guild.voice_client
        if vc is None:
            vc = await channel.connect()
        elif vc.channel != channel:
            await vc.move_to(channel)
        gq.voice_client = vc
        gq.text_channel = interaction.channel

        was_playing = vc.is_playing() or vc.is_paused()

        # Enqueue all tracks — the url field stores the local file path
        # so from_query() will pick it up from the audio_cache or we
        # override playback directly
        for t in valid_tracks:
            gq.add(SongEntry(
                title=t["title"],
                url=t["file_path"],  # local path — triggers local playback
                duration=t["duration"] or 0,
                requester=interaction.user,
            ))

        if not was_playing:
            from cogs.music import Music
            music_cog: Music = interaction.client.cogs.get("Music")
            if music_cog:
                await music_cog._play_next(interaction.guild_id)

        skipped = len(tracks) - len(valid_tracks)
        desc = (
            f"{'Added' if was_playing else 'Started playing'} "
            f"**{len(valid_tracks)}** track(s) from cached playlist **{name}**."
        )
        if skipped:
            desc += f"\n({skipped} track(s) skipped — files missing)"

        embed = discord.Embed(
            title=f"Local Playlist: {name}",
            description=desc,
            color=discord.Color.green(),
        )
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(CacheCog(bot))
