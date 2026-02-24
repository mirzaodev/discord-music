import asyncio
import copy
import os
import discord
import yt_dlp
from yt_dlp.utils import DownloadError

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

def _is_url(query: str) -> bool:
    """Return True if query looks like a URL rather than a search term."""
    return query.startswith(("http://", "https://", "www."))


def _extract_info_with_fallback(query: str) -> dict:
    """
    Try multiple yt-dlp option profiles to survive YouTube format/client
    breakages. Raises the last error if all profiles fail.
    """
    profiles = [
        {
            "format": "bestaudio/best",
            "extractor_args": {"youtube": {"player_client": ["ios", "web"]}},
        },
        {
            "format": "bestaudio*/best",
            "extractor_args": {"youtube": {"player_client": ["ios", "web"]}},
        },
        {
            "format": "best",
            "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
        },
        {
            "format": "best",
            "extractor_args": {},
        },
    ]

    last_error = None
    for profile in profiles:
        opts = copy.deepcopy(_YTDL_OPTIONS)
        opts["format"] = profile["format"]
        if profile["extractor_args"]:
            opts["extractor_args"] = profile["extractor_args"]
        else:
            opts.pop("extractor_args", None)

        try:
            return yt_dlp.YoutubeDL(opts).extract_info(query, download=False)
        except DownloadError as exc:
            last_error = exc
            message = str(exc)
            if "Requested format is not available" not in message:
                continue
        except Exception as exc:
            last_error = exc
            continue

    # -- SoundCloud fallback for search queries --
    if not _is_url(query):
        sc_opts = copy.deepcopy(_YTDL_OPTIONS)
        sc_opts["default_search"] = "scsearch"
        sc_opts.pop("extractor_args", None)
        sc_opts["format"] = "bestaudio/best"
        try:
            data = yt_dlp.YoutubeDL(sc_opts).extract_info(query, download=False)
            if data:
                data["_fallback_source"] = "SoundCloud"
                return data
        except Exception:
            pass  # SoundCloud also failed, raise the original YouTube error

    if last_error:
        raise last_error
    raise ValueError(f"Could not retrieve audio for: {query}")


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
        self.source: str = data.get("_fallback_source", "YouTube")

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
            lambda: _extract_info_with_fallback(query),
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
            lambda: _extract_info_with_fallback(query),
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
            "source": data.get("_fallback_source", "YouTube"),
        }
