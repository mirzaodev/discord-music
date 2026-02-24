import asyncio
import json
import subprocess
import sys
import os
import discord
import yt_dlp

_YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "outtmpl": "%(extractor)s-%(id)s-%(title)s.%(ext)s",
    "restrictfilenames": True,
    "noplaylist": True,
    "nocheckcertificate": True,
    "ignoreerrors": False,
    "logtostderr": False,
    "quiet": True,
    "no_warnings": True,
    "default_search": "scsearch",
    "source_address": "0.0.0.0",
    "geo_bypass": True,
}

FFMPEG_OPTIONS = {
    "before_options": (
        "-reconnect 1 "
        "-reconnect_streamed 1 "
        "-reconnect_delay_max 5"
    ),
    "options": "-vn",
}


_YTDL_PLAYLIST_OPTIONS = {**_YTDL_OPTIONS, "noplaylist": False, "ignoreerrors": True}


def _extract_info(query: str) -> dict:
    """Extract info via yt-dlp using SoundCloud search as default."""
    data = yt_dlp.YoutubeDL(_YTDL_OPTIONS).extract_info(query, download=False)
    if data is None:
        raise ValueError(f"Could not retrieve audio for: {query}")
    return data


# Helper script run as a subprocess for playlist extraction.
# Running in a separate process avoids GIL contention that causes audio stutter.
_PLAYLIST_WORKER = """
import json, sys, yt_dlp
opts = json.loads(sys.argv[1])
url  = sys.argv[2]
data = yt_dlp.YoutubeDL(opts).extract_info(url, download=False)
if data is None:
    print(json.dumps({"entries": []}))
    sys.exit(0)
entries = data.get("entries") or []
results = []
for e in entries:
    if e is None:
        continue
    results.append({
        "title": e.get("title", "Unknown"),
        "url":   e.get("webpage_url") or e.get("url"),
        "duration": int(e.get("duration") or 0),
        "thumbnail": e.get("thumbnail", ""),
    })
print(json.dumps(results))
"""


class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(
        self,
        source: discord.FFmpegPCMAudio,
        *,
        data: dict,
        volume: float = 0.5,
    ):
        super().__init__(source, volume=volume)
        self.data = data
        self.title: str = data.get("title", "Unknown")
        self.url: str = data["url"]
        self.webpage_url: str = data.get("webpage_url", data["url"])
        self.duration: int = int(data.get("duration") or 0)
        self.thumbnail: str = data.get("thumbnail", "")
        self.uploader: str = data.get("uploader", "Unknown")

    @classmethod
    async def from_query(
        cls,
        query: str,
        *,
        loop: asyncio.AbstractEventLoop = None,
    ) -> "YTDLSource":
        """
        Resolve a search query or URL to a playable YTDLSource.
        Runs yt-dlp in a thread pool so it doesn't block the event loop.
        Call this at play-time so CDN stream URLs are always fresh.
        """
        loop = loop or asyncio.get_event_loop()
        data = await loop.run_in_executor(
            None,
            lambda: _extract_info(query),
        )
        if "entries" in data:
            data = data["entries"][0]
        return cls(
            discord.FFmpegPCMAudio(data["url"], **FFMPEG_OPTIONS),
            data=data,
        )

    @classmethod
    async def fetch_metadata_only(
        cls,
        query: str,
        loop: asyncio.AbstractEventLoop = None,
    ) -> dict:
        """
        Resolve title, canonical URL, duration and thumbnail without creating
        an audio source. Use this at enqueue time; re-resolve at play time via
        from_query() to keep CDN URLs fresh.
        """
        loop = loop or asyncio.get_event_loop()
        data = await loop.run_in_executor(
            None,
            lambda: _extract_info(query),
        )
        if "entries" in data:
            data = data["entries"][0]
        return {
            "title": data.get("title", "Unknown"),
            "url": data.get("webpage_url") or data.get("url"),
            "duration": int(data.get("duration") or 0),
            "thumbnail": data.get("thumbnail", ""),
        }

    @classmethod
    async def fetch_playlist_metadata(
        cls,
        url: str,
        loop: asyncio.AbstractEventLoop = None,
    ) -> list[dict]:
        """
        Extract metadata for every track in a playlist URL.
        Runs yt-dlp in a separate *process* to avoid GIL contention
        that causes audio stuttering during long extractions.
        Returns a list of dicts with title/url/duration/thumbnail.
        """
        opts_json = json.dumps(_YTDL_PLAYLIST_OPTIONS)
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", _PLAYLIST_WORKER, opts_json, url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise ValueError(
                f"Playlist extraction failed: {stderr.decode(errors='replace')}"
            )
        return json.loads(stdout.decode())
