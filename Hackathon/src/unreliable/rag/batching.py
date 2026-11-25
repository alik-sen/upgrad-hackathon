"""Dynamic micro-batching (batch<=32, max_wait=8 ms): coalesces concurrent model calls into one ONNX run."""
from __future__ import annotations

import asyncio
import time
from concurrent.futures import Executor
from typing import Any, Callable


class MicroBatcher:
    def __init__(self, fn: Callable[[list[Any]], list[Any]], executor: Executor, max_batch: int = 32, max_wait_ms: float = 8.0):
        self.fn, self.ex, self.max_batch, self.max_wait = fn, executor, max_batch, max_wait_ms / 1000
        self.q: asyncio.Queue | None = None
        self.task: asyncio.Task | None = None
        self.batches = 0
        self.items = 0
        self.inflight = 0

    async def submit(self, item):
        if self.q is None:
            self.q = asyncio.Queue()
            self.task = asyncio.create_task(self._run())
        fut = asyncio.get_running_loop().create_future()
        self.inflight += 1
        await self.q.put((item, fut))
        try:
            return await fut
        finally:
            self.inflight -= 1

    async def _run(self):
        loop = asyncio.get_running_loop()
        while True:
            item, fut = await self.q.get()
            batch = [(item, fut)]
            # adaptive: the max_wait window only opens when other requests are in flight (never delay a lone request)
            deadline = time.perf_counter() + (self.max_wait if self.inflight > 1 or not self.q.empty() else 0.0)
            while len(batch) < self.max_batch:
                timeout = deadline - time.perf_counter()
                if timeout <= 0:
                    break
                try:
                    batch.append(await asyncio.wait_for(self.q.get(), timeout))
                except asyncio.TimeoutError:
                    break
            self.batches += 1; self.items += len(batch)
            try:
                results = await loop.run_in_executor(self.ex, self.fn, [b[0] for b in batch])
                for (_, f), r in zip(batch, results):
                    if not f.done():
                        f.set_result(r)
            except Exception as e:  # noqa: BLE001
                for _, f in batch:
                    if not f.done():
                        f.set_exception(e)
