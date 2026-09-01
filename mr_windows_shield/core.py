from potehinsonnet.net import NatsClient
from mr_windows_shield import config
from potehinsonnet.net_models.discord_models import NatsMessage, DiscordMessage,Placeholder,PlaceholderType

import re
from dotenv import load_dotenv
import os

load_dotenv()

LINUX_WORDS = [
    word.strip().lower()
    for word in os.getenv("LINUX_WORDS", "").split(",")
    if word.strip()
]

class Core:
    def __init__(self,nats_client:NatsClient):
        self.nats_client = nats_client
    async def start(self):
        await self.nats_client.subscribe("discord.message.get",self.on_message)

    async def send_message(self,message:NatsMessage):
        await self.nats_client.publish("discord.message.send",message.model_dump(mode="json"))

    async def on_message(self,message:dict):
        msg = DiscordMessage.model_validate(message)
        for word in LINUX_WORDS:
            if re.search(rf"\b{re.escape(word)}\b", msg.text):
                await self.send_message(
                    NatsMessage(service=config.PLUGIN_NAME,
                                placeholders={
                                    "user": Placeholder(type=PlaceholderType.MENTION,value=msg.user_id),
                                    "tag":Placeholder(type=PlaceholderType.TEXT,value=config.PLUGIN_TITLE),
                                    "source":Placeholder(type=PlaceholderType.TEXT,value=msg.text),
                                    "word":Placeholder(type=PlaceholderType.TEXT,value=word),
                                },
                                channel_id= msg.channel_id,
                                text= f"%tag%\nВ чате обнаружена подозрительная Linux активность, необходимо предпринять меры. \n Источник: %user% \n Инцидент: В `%source%` обнаружено `%word%` \n Предпринятые меры: Страпонинг"
                                )
                )
                break
        return
