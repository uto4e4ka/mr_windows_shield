import asyncio
from potehinsonnet.net import NatsClient
from potehinsonnet.net_models import system_models


async def main():
    nats_client = NatsClient("nats://localhost:4222")

    await nats_client.connect()
    print("✅ NATS connected")

    async def handler(data: dict):
        print("📩 RECEIVED:", data)

    await nats_client.subscribe("system.info", handler)
    print("👂 subscribed")

    await nats_client.publish(
        "system.info",
        system_models.service_info_model("test","New")
    )
    print("📤 published")

    # Даём callback время выполниться
    await asyncio.sleep(1)

    await nats_client.close()


asyncio.run(main())