from preyherbtracker.bot_app import build_bot
from preyherbtracker.config import load_settings


def main() -> None:
    settings = load_settings()
    if not settings.discord_token:
        raise RuntimeError("DISCORD_TOKEN is missing. Copy .env.example to .env and set your bot token.")
    build_bot(settings).run(settings.discord_token)


if __name__ == "__main__":
    main()
