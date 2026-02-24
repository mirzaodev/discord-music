import os
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN: str = os.environ["DISCORD_TOKEN"]
GUILD_ID: int | None = int(os.environ["GUILD_ID"]) if os.environ.get("GUILD_ID") else None
