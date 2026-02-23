import asyncio
import random
from collections import deque
from dataclasses import dataclass, field
from typing import Optional
import discord


@dataclass
class SongEntry:
    title: str
    url: str           # canonical YouTube watch URL (re-resolved fresh at play time)
    duration: int      # seconds
    requester: discord.Member
    thumbnail: Optional[str] = None


class GuildQueue:
    def __init__(self):
        self._queue: deque[SongEntry] = deque()
        self.current: Optional[SongEntry] = None
        self.voice_client: Optional[discord.VoiceClient] = None
        self._lock: asyncio.Lock = asyncio.Lock()

    def add(self, entry: SongEntry) -> None:
        """Append to end of queue."""
        self._queue.append(entry)

    def add_next(self, entry: SongEntry) -> None:
        """Insert at position 0 — plays immediately after the current song."""
        self._queue.appendleft(entry)

    def pop_next(self) -> Optional[SongEntry]:
        """Remove and return the next song to play."""
        if self._queue:
            return self._queue.popleft()
        return None

    def skip(self) -> None:
        """Stop current audio; the after= callback drives the next song."""
        if self.voice_client and self.voice_client.is_playing():
            self.voice_client.stop()

    def clear(self) -> None:
        self._queue.clear()
        self.current = None

    def shuffle(self) -> None:
        """Randomly reorder the upcoming queue (does not affect the current song)."""
        items = list(self._queue)
        random.shuffle(items)
        self._queue = deque(items)

    def list_entries(self) -> list[SongEntry]:
        return list(self._queue)

    def __len__(self) -> int:
        return len(self._queue)


class QueueManager:
    """Module-level singleton: maps guild_id → GuildQueue."""

    def __init__(self):
        self._guilds: dict[int, GuildQueue] = {}

    def get(self, guild_id: int) -> GuildQueue:
        if guild_id not in self._guilds:
            self._guilds[guild_id] = GuildQueue()
        return self._guilds[guild_id]

    def remove(self, guild_id: int) -> None:
        self._guilds.pop(guild_id, None)


# Singleton instance shared across cogs
queue_manager = QueueManager()
