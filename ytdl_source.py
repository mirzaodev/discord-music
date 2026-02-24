import asyncio
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
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",   # bind to IPv4, avoids IPv6 issues on some VPS
    "geo_bypass": True,
    "extractor_args": {
        "youtube": {
            "player_client": ["ios", "web"],
        }
    },
}

_cookies = os.environ.get("COOKIES_FILE")
if _cookies and os.path.isfile(_cookies):
    _YTDL_OPTIONS["cookiefile"] = _cookies

FFMPEG_OPTIONS = {
    "before_options": (
        "-reconnect 1 "
        "-reconnect_streamed 1 "
        "-reconnect_delay_max 5"
    ),
    "options": "-vn",
}

ytdl = yt_dlp.YoutubeDL(_YTDL_OPTIONS)


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
        self.duration: int = data.get("duration") or 0
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
            lambda: ytdl.extract_info(query, download=False),
        )
        if data is None:
            raise ValueError(f"Could not retrieve audio for: {query}")
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
            lambda: ytdl.extract_info(query, download=False),
        )
        if data is None:
            raise ValueError(f"Nothing found for: {query}")
        if "entries" in data:
            data = data["entries"][0]
        return {
            "title": data.get("title", "Unknown"),
            "url": data.get("webpage_url") or data.get("url"),
            "duration": data.get("duration") or 0,
            "thumbnail": data.get("thumbnail", ""),
        }
