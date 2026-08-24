"""Task helpers."""

from __future__ import annotations

import asyncio


async def end_task(task: asyncio.Task) -> None:
    """Cancel ``task`` and wait for it, re-raising a cancellation of the caller."""
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            raise
    except Exception:
        pass
