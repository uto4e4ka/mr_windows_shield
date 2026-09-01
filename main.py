import asyncio

from potehinsonnet.net import NatsClient
from potehinsonnet.net_models import system_models
from potehinsonnet.net_models import discord_models
PLUGIN_NAME = "mr_windows_shield"
async def main():
    nats_client = NatsClient("localhost")
    await nats_client.connect()
    await nats_client.publish("system.info",system_models.service_info_model(PLUGIN_NAME,"ENABLED✅"))
    await nats_client.publish("discord.message.send",discord_models.NatsMessage(
        text= "Hello world %user%",
        placeholders={
          "user":discord_models.Placeholder(
              type= discord_models.PlaceholderType.MENTION,
              value = 24242
          )
        },
        service= PLUGIN_NAME,
    ).model_dump(mode="json"))
asyncio.run(main())
