import sqlite3
import threading
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).parent / "music.db"

_local = threading.local()


def _get_conn() -> sqlite3.Connection:
    """Return a thread-local SQLite connection."""
    if not hasattr(_local, "conn"):
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        _local.conn = conn
    return _local.conn


def init_db() -> None:
    """Create tables on first run."""
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS playlists (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id   TEXT    NOT NULL,
            name       TEXT    NOT NULL,
            created_at TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE(guild_id, name)
        );

        CREATE TABLE IF NOT EXISTS playlist_songs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
            position    INTEGER NOT NULL,
            title       TEXT    NOT NULL,
            url         TEXT    NOT NULL,
            duration    INTEGER DEFAULT 0,
            UNIQUE(playlist_id, position)
        );

        CREATE INDEX IF NOT EXISTS idx_playlists_guild ON playlists(guild_id);
        CREATE INDEX IF NOT EXISTS idx_songs_playlist ON playlist_songs(playlist_id, position);

        CREATE TABLE IF NOT EXISTS audio_cache (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            url         TEXT    NOT NULL UNIQUE,
            file_path   TEXT    NOT NULL,
            title       TEXT    NOT NULL,
            duration    INTEGER DEFAULT 0,
            file_size   INTEGER DEFAULT 0,
            cached_at   TEXT    NOT NULL DEFAULT (datetime('now')),
            last_played TEXT    NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_cache_url ON audio_cache(url);
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# Playlist CRUD
# ---------------------------------------------------------------------------

def create_playlist(guild_id: str, name: str) -> int:
    """Insert a new playlist. Raises sqlite3.IntegrityError if name already exists."""
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO playlists (guild_id, name) VALUES (?, ?)",
        (guild_id, name),
    )
    conn.commit()
    return cur.lastrowid


def get_playlist(guild_id: str, name: str) -> Optional[sqlite3.Row]:
    conn = _get_conn()
    return conn.execute(
        "SELECT * FROM playlists WHERE guild_id = ? AND name = ?",
        (guild_id, name),
    ).fetchone()


def list_playlists(guild_id: str) -> list:
    conn = _get_conn()
    return conn.execute(
        "SELECT p.id, p.name, COUNT(s.id) AS song_count "
        "FROM playlists p "
        "LEFT JOIN playlist_songs s ON s.playlist_id = p.id "
        "WHERE p.guild_id = ? "
        "GROUP BY p.id ORDER BY p.name",
        (guild_id,),
    ).fetchall()


def delete_playlist(guild_id: str, name: str) -> bool:
    conn = _get_conn()
    deleted = conn.execute(
        "DELETE FROM playlists WHERE guild_id = ? AND name = ?",
        (guild_id, name),
    ).rowcount
    conn.commit()
    return bool(deleted)


# ---------------------------------------------------------------------------
# Song CRUD
# ---------------------------------------------------------------------------

def add_song_to_playlist(
    playlist_id: int,
    title: str,
    url: str,
    duration: int,
) -> None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT COALESCE(MAX(position) + 1, 0) AS next_pos "
        "FROM playlist_songs WHERE playlist_id = ?",
        (playlist_id,),
    ).fetchone()
    conn.execute(
        "INSERT INTO playlist_songs (playlist_id, position, title, url, duration) "
        "VALUES (?, ?, ?, ?, ?)",
        (playlist_id, row["next_pos"], title, url, duration),
    )
    conn.commit()


def remove_song_from_playlist(playlist_id: int, index: int) -> bool:
    """Remove the song at 0-based index and compact positions. Returns True if deleted."""
    conn = _get_conn()
    with conn:
        deleted = conn.execute(
            "DELETE FROM playlist_songs WHERE playlist_id = ? AND position = ?",
            (playlist_id, index),
        ).rowcount
        if deleted:
            conn.execute(
                "UPDATE playlist_songs SET position = position - 1 "
                "WHERE playlist_id = ? AND position > ?",
                (playlist_id, index),
            )
    return bool(deleted)


def get_playlist_songs(playlist_id: int) -> list:
    conn = _get_conn()
    return conn.execute(
        "SELECT * FROM playlist_songs WHERE playlist_id = ? ORDER BY position",
        (playlist_id,),
    ).fetchall()


# ---------------------------------------------------------------------------
# Audio cache CRUD
# ---------------------------------------------------------------------------

def get_cached_track(url: str) -> Optional[sqlite3.Row]:
    conn = _get_conn()
    return conn.execute(
        "SELECT * FROM audio_cache WHERE url = ?", (url,)
    ).fetchone()


def upsert_cached_track(
    url: str, file_path: str, title: str, duration: int, file_size: int
) -> None:
    conn = _get_conn()
    conn.execute(
        "INSERT INTO audio_cache (url, file_path, title, duration, file_size) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(url) DO UPDATE SET "
        "file_path=excluded.file_path, title=excluded.title, "
        "duration=excluded.duration, file_size=excluded.file_size, "
        "last_played=datetime('now')",
        (url, file_path, title, duration, file_size),
    )
    conn.commit()


def touch_cached_track(url: str) -> None:
    conn = _get_conn()
    conn.execute(
        "UPDATE audio_cache SET last_played = datetime('now') WHERE url = ?",
        (url,),
    )
    conn.commit()


def delete_cached_track(url: str) -> None:
    conn = _get_conn()
    conn.execute("DELETE FROM audio_cache WHERE url = ?", (url,))
    conn.commit()


def get_all_cached_tracks() -> list:
    conn = _get_conn()
    return conn.execute(
        "SELECT * FROM audio_cache ORDER BY last_played ASC"
    ).fetchall()


def get_total_cache_size() -> int:
    conn = _get_conn()
    row = conn.execute("SELECT COALESCE(SUM(file_size), 0) AS total FROM audio_cache").fetchone()
    return row["total"]
