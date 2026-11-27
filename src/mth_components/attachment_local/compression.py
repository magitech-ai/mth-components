"""Instance-owned concurrency bound for native image transformations.

Ports ``packages/attachment/attachment-local/src/compression-limiter.ts``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

T = TypeVar("T")


def compression_failure(reason: Any) -> BaseException:
    """Keep real exceptions and normalize non-error native failures."""
    return (
        reason
        if isinstance(reason, BaseException)
        else RuntimeError("Image compression task rejected with a non-Error value.")
    )


class CompressionLimiter:
    """Run one image task after an instance slot becomes available."""

    def __init__(self, concurrency: int) -> None:
        if (
            not isinstance(concurrency, int)
            or isinstance(concurrency, bool)
            or concurrency < 1
        ):
            raise ValueError("imageCompressionConcurrency must be a positive integer")
        self.concurrency = concurrency
        self._semaphore = asyncio.Semaphore(concurrency)

    async def run(self, task: Callable[[], Awaitable[T]]) -> T:
        async with self._semaphore:
            return await task()


__all__ = ["CompressionLimiter", "compression_failure"]
