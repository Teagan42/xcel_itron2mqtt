from typing import Awaitable, Callable

PublishFn = (
    Callable[[str, bytes, int | None, bool | None, int | None], Awaitable[None]] |
    Callable[[str, bytes, int | None, bool | None], Awaitable[None]] |
    Callable[[str, bytes, int | None], Awaitable[None]] |
    Callable[[str, bytes], Awaitable[None]]
)

class TerminateTaskGroup(Exception):
    """Exception raised to terminate a task group."""

async def force_terminate_task_group():
    """Used to force termination of a task group."""
    raise TerminateTaskGroup()