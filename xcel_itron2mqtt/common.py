from typings import Awaitable, Callable
from aiomqtt.types import PayloadType
from paho.mqtt.matcher import MQTTMessageInfo

PublishFn = 
        /,
        topic: str,
        payload: PayloadType = None,
        qos: int = 0,
        retain: bool = False,  # noqa: FBT001, FBT002
        properties: Properties | None = None,
        *args: Any,  # noqa: ANN401
        timeout: float | None = None,
        **kwargs: Any,  # noqa: ANN401
    ) -> None:

PublishFn = Callable[[str, str | bytes | bytearray | int | float | None = None, int = 0, bool = False, Properties | None = None], Awaitable[MQTTMessageInfo]]

class TerminateTaskGroup(Exception):
    """Exception raised to terminate a task group."""

async def force_terminate_task_group():
    """Used to force termination of a task group."""
    raise TerminateTaskGroup()