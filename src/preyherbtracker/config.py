from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os

from dotenv import load_dotenv


@dataclass(slots=True)
class Settings:
    discord_token: str
    database_path: Path
    bot_guild_id: int | None = None



def load_settings() -> Settings:
    load_dotenv()
    discord_token = os.getenv("DISCORD_TOKEN", "").strip()
    database_path = Path(os.getenv("DATABASE_PATH", "data/preyherbtracker.sqlite3")).resolve()
    guild_value = os.getenv("BOT_GUILD_ID", "").strip()
    bot_guild_id = int(guild_value) if guild_value else None
    return Settings(
        discord_token=discord_token,
        database_path=database_path,
        bot_guild_id=bot_guild_id,
    )
