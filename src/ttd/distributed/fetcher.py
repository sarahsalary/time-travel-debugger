"""Bridge between the distributed coordinator and the Loki fetcher.

Each distributed chunk is a fixed time range. The worker fetches it
using the existing sequential Loki client, writes the events to its
private file, and marks the lease complete.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from .coordinator import DistributedCoordinator
from .lease import ChunkLease


@dataclass
class DistributedFetcher:
    """Drive a worker through its share of a distributed job."""

    coordinator: DistributedCoordinator
    client: object  # LokiClient or LokiPoolClient
    logql: str
    max_events_per_chunk: int | None = None
    on_chunk_complete: Callable[[ChunkLease, int], None] | None = None
    on_chunk_failed: Callable[[ChunkLease, str], None] | None = None

    def run(self, *, max_chunks: int | None = None) -> dict:
        """Process chunks until the job is done or ``max_chunks`` is hit."""
        processed = 0
        total_events = 0
        failed_chunks = 0
        max_failures = 5

        while True:
            if max_chunks is not None and processed >= max_chunks:
                break

            chunk = self.coordinator.next_chunk()
            if chunk is None:
                break

            start = datetime.fromisoformat(chunk.start)
            end = datetime.fromisoformat(chunk.end)

            try:
                events = list(
                    self.client.stream_range(
                        self.logql,
                        start=start,
                        end=end,
                        max_events=self.max_events_per_chunk,
                    )
                )
                events = [e for e in events if start <= e.timestamp < end]
            except Exception as exc:
                self.coordinator.fail_chunk(chunk, str(exc))
                failed_chunks += 1
                if failed_chunks >= max_failures:
                    break
                if self.on_chunk_failed is not None:
                    self.on_chunk_failed(chunk, str(exc))
                continue

            self.coordinator.complete_chunk(chunk, events)
            total_events += len(events)
            processed += 1
            if self.on_chunk_complete is not None:
                self.on_chunk_complete(chunk, len(events))

        return {
            "worker_id": self.coordinator.worker_id,
            "chunks_processed": processed,
            "events_fetched": total_events,
            "chunks_failed": failed_chunks,
        }