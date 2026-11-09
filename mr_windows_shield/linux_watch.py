"""Слежка за Linux-активностью в чате: нашли слово из списка — бот поднимает тревогу."""
import re
from contextlib import asynccontextmanager

from potehinsonnet.discord_provider import DiscordProvider
from potehinsonnet.net import NatsClient
from potehinsonnet.net_models.discord_models import DiscordMessage, NatsMessage, Placeholder, PlaceholderType

from mr_windows_shield import config


def parse_words(raw: str) -> list[str]:
    return [word.strip().lower() for word in raw.split(",") if word.strip()]


def find_word(text: str, words: list[str]) -> str | None:
    return next((word for word in words if re.search(rf"\b{re.escape(word)}\b", text.lower())), None)


class LinuxWatch:
    def __init__(self, nats: NatsClient, discord: DiscordProvider, words: list[str]):
        self.nats = nats
        self.discord = discord
        self.words = words

    async def on_message(self, body: dict) -> None:
        msg = DiscordMessage.model_validate(body)
        word = find_word(msg.text, self.words)
        if word is None:
            return
        await self.discord.send_message(NatsMessage(
            service=config.PLUGIN_NAME,
            channel_id=msg.channel_id,
            placeholders={
                "user": Placeholder(type=PlaceholderType.MENTION, value=msg.user.id),
                "tag": Placeholder(type=PlaceholderType.TEXT, value=config.PLUGIN_TITLE),
                "source": Placeholder(type=PlaceholderType.TEXT, value=msg.text),
                "word": Placeholder(type=PlaceholderType.TEXT, value=word),
            },
            text="%tag%\nВ чате обнаружена подозрительная Linux активность, необходимо предпринять меры. \n"
                 " Источник: %user% \n Инцидент: В `%source%` обнаружено `%word%` \n Предпринятые меры: Страпонинг",
        ))

    @asynccontextmanager
    async def running(self):
        sub = await self.nats.subscribe("discord.message.get", self.on_message)
        try:
            yield self
        finally:
            await sub.unsubscribe()
