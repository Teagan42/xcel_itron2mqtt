import asyncio
from typing import Callable
import logging
from uuid import uuid4
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
        self._connect_lock = asyncio.Lock()
        self.client = MQTTClient(
            client_id or username or uuid4().hex,
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
                "protocol": "MQTTv311",
                "broker": {
                    "uri": f'mqtt{"s" if port == 8883 else ""}://{username if username else ""}:{password if password else ""}@{broker}:{port}',
                },
            },
        )
        original_disconnect = self.client.disconnect

        async def traced_disconnect(*args, **kwargs):
            logger.warning("AMQTT client initiated disconnect")
            return await original_disconnect(*args, **kwargs)

        self.client.disconnect = traced_disconnect
        self._watchdog_task = None
        self.connected = False

    async def _watchdog(self):
        logger.info("MQTT watchdog started")
        try:
            while True:
                await asyncio.sleep(30)
                try:
                    await self.client.ping()
                    logger.debug("MQTT watchdog ping successful")
                except Exception as e:
                    logger.warning(f"MQTT watchdog ping failed: {e}")
        except asyncio.CancelledError:
            logger.info("MQTT watchdog task cancelled")

    def start_watchdog(self):
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = asyncio.create_task(self._watchdog())

    async def connect(self) -> None:
        try:
            async with self._connect_lock:
                if self.connected:
                    return
                logger.info("Connecting to MQTT broker...")
                await self.client.connect()
                self.connected = True
                self.start_watchdog()
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
        async with self._connect_lock:
            if self.connected:
                if self._watchdog_task:
                    self._watchdog_task.cancel()
                    try:
                        await self._watchdog_task
                    except asyncio.CancelledError:
                        pass
                await self.client.disconnect()
                self.connected = False
            else:
                print("Client is not connected, cannot disconnect.")
