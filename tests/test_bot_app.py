import unittest
from unittest.mock import patch

from preyherbtracker import bot_app


class _FakeForumChannel:
    pass


class _FakeThread:
    def __init__(self, parent=None) -> None:
        self.parent = parent

    async def fetch_message(self, message_id: int):
        raise LookupError(message_id)


class _FakeTextChannel:
    def __init__(self, *, name: str = "general", messages=None) -> None:
        self.id = 222222222222222222
        self.name = name
        self._messages = messages or {}

    async def fetch_message(self, message_id: int):
        if message_id not in self._messages:
            raise LookupError(message_id)
        return self._messages[message_id]


class _FakeMessage:
    def __init__(self, *, message_id: int = 333333333333333333, content: str = "hello") -> None:
        self.id = message_id
        self.content = content
        self.embeds = []
        self.attachments = []


class _FakeGuild:
    def __init__(self, *, guild_id: int = 111111111111111111, channels=None, threads=None, fetched=None) -> None:
        self.id = guild_id
        self._channels = channels or {}
        self._threads = threads or {}
        self._fetched = fetched or {}

    def get_channel(self, channel_id: int):
        return self._channels.get(channel_id)

    def get_thread(self, thread_id: int):
        return self._threads.get(thread_id)

    async def fetch_channel(self, channel_id: int):
        return self._fetched.get(channel_id)


class BotAppTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolve_thread_from_link_fetches_uncached_thread(self) -> None:
        forum = _FakeForumChannel()
        thread = _FakeThread(parent=forum)
        guild = _FakeGuild(
            fetched={
                111111111111111111: object(),
                222222222222222222: object(),
                333333333333333333: thread,
            }
        )
        link = "https://discord.com/channels/111111111111111111/222222222222222222/333333333333333333"

        with patch.object(bot_app.discord, "Thread", _FakeThread):
            resolved = await bot_app.resolve_thread_from_link(guild, link)

        self.assertIs(resolved, thread)

    async def test_resolve_thread_from_link_rejects_non_thread_targets(self) -> None:
        guild = _FakeGuild(fetched={333333333333333333: _FakeForumChannel()})

        with patch.object(bot_app.discord, "Thread", _FakeThread):
            with self.assertRaises(ValueError):
                await bot_app.resolve_thread_from_link(guild, "333333333333333333")

    async def test_resolve_message_from_link_fetches_message_from_channel(self) -> None:
        message = _FakeMessage(content="tracked item")
        channel = _FakeTextChannel(messages={333333333333333333: message})
        guild = _FakeGuild(channels={222222222222222222: channel})
        link = "https://discord.com/channels/111111111111111111/222222222222222222/333333333333333333"

        with patch.object(bot_app.discord.abc, "GuildChannel", _FakeTextChannel), patch.object(bot_app.discord, "Thread", _FakeThread):
            resolved_channel, resolved_message = await bot_app.resolve_message_from_link(guild, link)

        self.assertIs(resolved_channel, channel)
        self.assertIs(resolved_message, message)

    async def test_resolve_message_from_link_rejects_other_guild(self) -> None:
        guild = _FakeGuild(guild_id=111111111111111111)
        link = "https://discord.com/channels/999999999999999999/222222222222222222/333333333333333333"

        with self.assertRaises(ValueError):
            await bot_app.resolve_message_from_link(guild, link)


if __name__ == "__main__":
    unittest.main()