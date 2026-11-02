"""Parallel Loki pagination with adaptive splitting and bounded work queues."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Any
import time
from .loki_backpressure import BackpressureConfig, ConcurrencyController, AdaptiveWorkPool

from .models import LogEvent


def parallel_stream_range(
    client: Any,
    logql: str,
    start: datetime,
    end: datetime,
    *,
    workers: int = 4,
    page_size: int = 1000,
    adaptive: bool = False,
    min_chunk_seconds: float = 10.0,
    target_events_per_chunk: int = 5000,
) -> list[LogEvent]:
    """Fetch disjoint half-open time ranges and preserve chronological order.

    Dynamic mode recursively subdivides dense chunks when safe. There is a
    finite minimum chunk size and no fabricated or unverified log records.
    """
    if workers < 1:
        raise ValueError("workers must be >= 1")
    if start >= end:
        return []
    seconds = (end - start).total_seconds()
    chunks = max(workers * 2, 1)
    spans = [
        (start + timedelta(seconds=seconds * i / chunks),
         start + timedelta(seconds=seconds * (i + 1) / chunks))
        for i in range(chunks)
    ]

    controller = ConcurrencyController(BackpressureConfig(
        initial_limit=min(2, workers),
        min_limit=1,
        max_limit=workers,
        measurement_window=2,
    ))
    gate = AdaptiveWorkPool(controller)

    def fetch(span: tuple[datetime, datetime], depth: int = 0) -> list[LogEvent]:
        a, b = span
        if a >= b:
            return []
        if not gate.wait_for_slot(timeout=600.0):
            raise TimeoutError("Adaptive Loki concurrency queue timed out")
        started = time.monotonic()
        try:
            batch = list(client.stream_range(logql, start=a, end=b))
        except Exception as exc:
            controller.record_failure(rate_limited=("429" in str(exc)))
            raise
        else:
            controller.record_success(time.monotonic() - started)
        finally:
            gate.release()
        batch = [e for e in batch if a <= e.timestamp < b]
        if adaptive and len(batch) > target_events_per_chunk and (b - a).total_seconds() > min_chunk_seconds * 2 and depth < 8:
            mid = a + (b - a) / 2
            return fetch((a, mid), depth + 1) + fetch((mid, b), depth + 1)
        return batch

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ttd-loki") as pool:
        futures = {pool.submit(fetch, span): span for span in spans}
        batches = [(span, future.result()) for future, span in futures.items()]
    batches.sort(key=lambda pair: pair[0][0])
    events = [e for _, batch in batches for e in batch]
    events.sort(key=lambda e: (int(e.extra.get("loki_timestamp_ns", 0)), e.timestamp))
    return events
