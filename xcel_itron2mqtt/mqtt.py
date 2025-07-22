from typing import Callable
import logging
from amqtt.client import MQTTClient

logger = logging.getLogger(__name__)

class Mqtt:
    def __init__(
            self,
            broker: str,
            port: int,
            client_id: str | None = None,
            username: str | None = None,
            password: str | None = None,
    ) -> None:
        self.client = MQTTClient(
            client_id,
            config={
                "clean_start": True,
                "keep_alive": 60,
                "ping_delay": 1,
                "default_qos": 0,
                "default_retain": False,
                "auto_reconnect": True,
                "reconnect_retries": 100,
                "reconnect_delay": 10,
                "reconnect_delay_max": 60,
                "reconnect_delay_jitter": 0.1,
                "will": None,
                "properties": None,
                "protocol": "MQTTv311",
                "broker": {
                    "uri": f'mqtt{"s" if port == 8883 else ""}://{username if username else ""}:{password if password else ""}@{broker}:{port}',
                },
            },
        )
        self.connected = False

    async def connect(self) -> None:
        try:
            await self.client.connect()
            self.connected = True
        except Exception as e:
            logger.error(f"Error connecting to MQTT broker: {e}", exc_info=True, stack_info=True)
            logger.error(e)

    async def publish(
            self,
            topic: str,
            payload: bytes,
            qos: int = 0,
            retain: bool = False
    ) -> None:
        if not self.connected:
            await self.connect()
        await self.client.publish(
            topic,
            payload,
            qos=qos,
            retain=retain
        )

    async def subscribe(
            self,
            topic: str,
            callback: Callable[[str, str], None],
            qos: int = 0
    ) -> None:
        if not self.connected:
            await self.connect()
        await self.client.subscribe(
            topic,
            qos=qos,
            callback=callback
        )

    async def disconnect(self) -> None:
        if self.connected:
            await self.client.disconnect()
            self.connected = False
        else:
            print("Client is not connected, cannot disconnect.")
