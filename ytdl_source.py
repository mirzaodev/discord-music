import asyncio
import copy
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


def _extract_playlist(url: str) -> dict:
    """Extract info with playlist support enabled."""
    data = yt_dlp.YoutubeDL(_YTDL_PLAYLIST_OPTIONS).extract_info(url, download=False)
    if data is None:
        raise ValueError(f"Could not retrieve playlist for: {url}")
    return data


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
        Returns a list of dicts with title/url/duration/thumbnail.
        Skips entries that failed to resolve (private, geo-blocked, etc.).
        """
        loop = loop or asyncio.get_event_loop()
        data = await loop.run_in_executor(
            None,
            lambda: _extract_playlist(url),
        )
        entries = data.get("entries") or []
        results = []
        for entry in entries:
            if entry is None:
                continue
            results.append({
                "title": entry.get("title", "Unknown"),
                "url": entry.get("webpage_url") or entry.get("url"),
                "duration": int(entry.get("duration") or 0),
                "thumbnail": entry.get("thumbnail", ""),
            })
        return results
