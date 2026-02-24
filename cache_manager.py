import os
from pathlib import Path
import hashlib

import database as db

CACHE_DIR = Path(__file__).parent / "audio_cache"
MAX_CACHE_SIZE_MB = 2000  # 2 GB


def url_to_hash(url: str) -> str:
    """Deterministic short hash of a canonical URL for use as a filename."""
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def get_cached_path(url: str) -> str | None:
    """Return the local file path if this URL is cached and the file exists."""
    row = db.get_cached_track(url)
    if row and os.path.isfile(row["file_path"]):
        db.touch_cached_track(url)
        return row["file_path"]
    # DB row exists but file was deleted externally — clean up
    if row:
        db.delete_cached_track(url)
    return None


def register_cached_file(
    url: str, file_path: str, title: str, duration: int
) -> None:
    """Register a newly downloaded file in the cache index."""
    file_size = os.path.getsize(file_path)
    db.upsert_cached_track(url, file_path, title, duration, file_size)
    enforce_cache_limit()


def enforce_cache_limit() -> None:
    """Evict least-recently-played tracks until cache is under the size limit."""
    max_bytes = MAX_CACHE_SIZE_MB * 1024 * 1024
    total = db.get_total_cache_size()
    if total <= max_bytes:
        return
    for track in db.get_all_cached_tracks():
        if total <= max_bytes:
            break
        try:
            os.remove(track["file_path"])
        except FileNotFoundError:
            pass
        total -= track["file_size"]
        db.delete_cached_track(track["url"])


def ensure_cache_dir() -> None:
    """Create the cache directory if it doesn't exist. Call at bot startup."""
    CACHE_DIR.mkdir(exist_ok=True)
