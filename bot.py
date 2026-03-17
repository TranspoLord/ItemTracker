from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from preyherbtracker.bot_app import build_bot
from preyherbtracker.config import load_settings


def main() -> None:
    settings = load_settings()
    if not settings.discord_token:
        raise RuntimeError("DISCORD_TOKEN is missing. Copy .env.example to .env and set your bot token.")
    bot = build_bot(settings)
    bot.run(settings.discord_token)


if __name__ == "__main__":
    main()
