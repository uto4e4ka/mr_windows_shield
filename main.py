import asyncio

from potehinsonnet.net import NatsClient
from potehinsonnet.monitoring.health import Health
from mr_windows_shield import config
from potehinsonnet.net_models import discord_models
from mr_windows_shield.core import Core

nats_client = NatsClient("localhost")

async def main():
    await nats_client.connect()
    Health(nats_client,config.PLUGIN_NAME)
    core = Core(nats_client)
    await core.start()
    await asyncio.Event().wait()

asyncio.run(main())
