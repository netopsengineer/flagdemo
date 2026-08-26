"""Measure asyncio event-loop scheduling delay during a benchmark run.

The monitor requests a wake-up every five milliseconds and records how late
the coroutine resumes. Elevated lag means the worker event loop could not run
the monitor on schedule. Possible causes include synchronous application work,
garbage collection, CPU saturation, host scheduling, or blocking provider
calls. Compare lag across controlled runs; do not attribute it to one cause
without the accompanying workload and resource evidence.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from typing import Any


class LoopMonitor:
    def __init__(self, interval_ms: float = 5.0, capacity: int = 200_000):
        self._interval = interval_ms / 1000.0
        self._samples: deque[float] = deque(maxlen=capacity)
        self._task: asyncio.Task[None] | None = None

    async def _run(self) -> None:
        while True:
            started = time.perf_counter()
            await asyncio.sleep(self._interval)
            lag_ms = (time.perf_counter() - started - self._interval) * 1000
            self._samples.append(max(lag_ms, 0.0))

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            raise RuntimeError("loop monitor is already running")
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    def reset(self) -> None:
        self._samples.clear()

    def stats(self) -> dict[str, Any]:
        samples = sorted(self._samples)
        if not samples:
            return {"samples": 0, "interval_ms": self._interval * 1000}

        def pct(p: float) -> float:
            index = min(int(len(samples) * p), len(samples) - 1)
            return round(samples[index], 3)

        return {
            "samples": len(samples),
            "interval_ms": self._interval * 1000,
            "p50_ms": pct(0.50),
            "p95_ms": pct(0.95),
            "p99_ms": pct(0.99),
            "max_ms": round(samples[-1], 3),
            "over_50ms": sum(1 for sample in samples if sample > 50),
            "over_250ms": sum(1 for sample in samples if sample > 250),
        }
