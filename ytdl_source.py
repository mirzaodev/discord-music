import asyncio
import os
import discord
import yt_dlp

import cache_manager
import database as db

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

LOCAL_FFMPEG_OPTIONS = {
    "options": "-vn",
}

_YTDL_PLAYLIST_OPTIONS = {**_YTDL_OPTIONS, "noplaylist": False, "ignoreerrors": True}


def _extract_info(query: str) -> dict:
    """Extract info via yt-dlp using SoundCloud search as default."""
    data = yt_dlp.YoutubeDL(_YTDL_OPTIONS).extract_info(query, download=False)
    if data is None:
        raise ValueError(f"Could not retrieve audio for: {query}")
    return data


def _extract_and_download(query: str) -> dict:
    """Extract info AND download audio to the cache directory."""
    file_hash = cache_manager.url_to_hash(query)
    outtmpl = str(cache_manager.CACHE_DIR / f"{file_hash}.%(ext)s")

    opts = {
        **_YTDL_OPTIONS,
        "outtmpl": outtmpl,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "opus",
            "preferredquality": "96",
        }],
    }

    data = yt_dlp.YoutubeDL(opts).extract_info(query, download=True)
    if data is None:
        raise ValueError(f"Could not retrieve audio for: {query}")

    # yt-dlp may change extension after post-processing
    expected_path = str(cache_manager.CACHE_DIR / f"{file_hash}.opus")
    data["_downloaded_file"] = expected_path
    return data


def _download_single_track(url: str, output_path: str) -> dict:
    """Download and encode a single track to a specific opus file path."""
    # Strip extension — yt-dlp adds it via postprocessor
    base = output_path.rsplit(".", 1)[0] if "." in output_path else output_path

    opts = {
        **_YTDL_OPTIONS,
        "outtmpl": base + ".%(ext)s",
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "opus",
            "preferredquality": "96",
        }],
    }

    data = yt_dlp.YoutubeDL(opts).extract_info(url, download=True)
    if data is None:
        raise ValueError(f"Could not retrieve audio for: {url}")
    data["_downloaded_file"] = base + ".opus"
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
        Checks the local audio cache first. On a miss, downloads and caches
        the file for future plays. Falls back to streaming on any error.
        """
        loop = loop or asyncio.get_event_loop()

        # 0. Direct local file path — used by /playlocal
        if os.path.isfile(query):
            print(f"[playback] LOCAL FILE: {query}")
            return cls(
                discord.FFmpegPCMAudio(query, **LOCAL_FFMPEG_OPTIONS),
                data={
                    "url": query,
                    "webpage_url": query,
                    "title": os.path.basename(query),
                    "duration": 0,
                    "thumbnail": "",
                    "uploader": "",
                },
            )

        # 1. Cache hit — play from local file
        cached_path = cache_manager.get_cached_path(query)
        if cached_path:
            row = db.get_cached_track(query)
            print(f"[playback] CACHE HIT: '{row['title']}' -> {cached_path}")
            data = {
                "url": cached_path,
                "webpage_url": query,
                "title": row["title"],
                "duration": row["duration"],
                "thumbnail": "",
                "uploader": "",
            }
            return cls(
                discord.FFmpegPCMAudio(cached_path, **LOCAL_FFMPEG_OPTIONS),
                data=data,
            )

        # 2. Cache miss — download to cache
        try:
            data = await loop.run_in_executor(
                None, lambda: _extract_and_download(query)
            )
            if "entries" in data:
                data = data["entries"][0]

            downloaded_file = data["_downloaded_file"]
            if os.path.isfile(downloaded_file):
                cache_manager.register_cached_file(
                    url=data.get("webpage_url") or query,
                    file_path=downloaded_file,
                    title=data.get("title", "Unknown"),
                    duration=int(data.get("duration") or 0),
                )
                data["url"] = downloaded_file
                print(f"[playback] DOWNLOADED & CACHED: '{data.get('title', 'Unknown')}' -> {downloaded_file}")
                return cls(
                    discord.FFmpegPCMAudio(downloaded_file, **LOCAL_FFMPEG_OPTIONS),
                    data=data,
                )
        except Exception as exc:
            print(f"[playback] Download failed, falling back to streaming: {exc}")

        # 3. Fallback — stream from CDN
        data = await loop.run_in_executor(
            None, lambda: _extract_info(query)
        )
        if "entries" in data:
            data = data["entries"][0]
        print(f"[playback] STREAMING: '{data.get('title', 'Unknown')}' from CDN")
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
