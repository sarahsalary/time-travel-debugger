"""Top-level coordinator that ties the shared store and leases together.

A worker process creates a ``DistributedCoordinator``, joins the job,
and repeatedly calls ``next_chunk()`` until no work remains. Each call
returns a claimed chunk that the worker is responsible for fetching and
writing back.
"""

from __future__ import annotations

import os
import socket
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .lease import ChunkLease, LeaseManager, LeaseStatus
from .shared_store import SharedCheckpointStore, SharedJobManifest


# ---------------------------------------------------------------------------
# Worker identity
# ---------------------------------------------------------------------------


@dataclass
class WorkerIdentity:
    """Stable identity for this worker process."""

    worker_id: str
    hostname: str
    pid: int

    @classmethod
    def auto(cls) -> "WorkerIdentity":
        host = socket.gethostname().split(".")[0]
        pid = os.getpid()
        # Add a short random suffix so two workers on the same host with
        # recycled PIDs never collide.
        suffix = uuid.uuid4().hex[:6]
        return cls(
            worker_id=f"{host}-{pid}-{suffix}",
            hostname=host,
            pid=pid,
        )


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


class DistributedCoordinator:
    """Coordinate one worker's participation in a distributed job.

    Usage::

        coord = DistributedCoordinator(
            root=Path("/shared/ttd"),
            job_id="incident-2024",
        )
        coord.join()
        while (chunk := coord.next_chunk()) is not None:
            try:
                events = fetch(chunk)
                coord.complete_chunk(chunk, events)
            except Exception as exc:
                coord.fail_chunk(chunk, str(exc))
    """

    def __init__(
        self,
        root: Path,
        job_id: str,
        *,
        lease_ttl_seconds: float = 300.0,
        worker: WorkerIdentity | None = None,
    ) -> None:
        self.root = Path(root)
        self.job_id = job_id
        self.lease_ttl_seconds = lease_ttl_seconds
        self.worker = worker or WorkerIdentity.auto()
        self.store = SharedCheckpointStore(self.root, job_id)
        self.leases = LeaseManager(
            self.store.lease_path,
            lease_ttl_seconds=lease_ttl_seconds,
        )
        self._manifest: SharedJobManifest | None = None

    # -- lifecycle ---------------------------------------------------------

    @property
    def worker_id(self) -> str:
        return self.worker.worker_id

    @property
    def manifest(self) -> SharedJobManifest | None:
        return self._manifest

    def initialize_job(
        self,
        *,
        query: str,
        start: datetime,
        end: datetime,
        mode: str,
        chunk_seconds: float,
    ) -> SharedJobManifest:
        """Create the job on shared storage (first worker only).

        Safe to call from every worker: the first one wins the race and
        the rest simply read the existing manifest.
        """
        existing = self.store.read_manifest()
        if existing is not None:
            self._manifest = existing
            return existing

        chunks = _plan_chunk_ids(start, end, chunk_seconds)
        manifest = SharedJobManifest(
            job_id=self.job_id,
            query=query,
            original_start=start.isoformat(),
            original_end=end.isoformat(),
            mode=mode,
            total_chunks=len(chunks),
            chunk_seconds=chunk_seconds,
        )
        self.store.write_manifest(manifest)
        self.leases.initialize(chunks)
        self._manifest = manifest
        return manifest

    def join(self) -> SharedJobManifest:
        """Join an existing job, or raise if the manifest is missing."""
        manifest = self.store.read_manifest()
        if manifest is None:
            raise FileNotFoundError(
                f"No shared job found at {self.store.job_dir}. "
                "Run initialize_job first."
            )
        self._manifest = manifest
        return manifest

    # -- work loop ---------------------------------------------------------

    def next_chunk(self) -> ChunkLease | None:
        """Claim the next available chunk, or return None when done."""
        return self.leases.claim_next(self.worker_id)

    def complete_chunk(
        self,
        chunk: ChunkLease,
        events: list,
    ) -> None:
        """Persist events for a completed chunk and release the lease."""
        lines = [
            event.model_dump_json() if hasattr(event, "model_dump_json")
            else str(event)
            for event in events
        ]
        out = self.store.save_chunk_events(chunk.chunk_id, lines)
        self.leases.complete(
            chunk.chunk_id,
            event_count=len(events),
            output_file=str(out),
        )

    def fail_chunk(self, chunk: ChunkLease, error: str) -> None:
        """Mark a chunk as failed so another worker can retry it."""
        self.leases.fail(chunk.chunk_id, error=error)

    def renew_lease(self, chunk: ChunkLease) -> bool:
        """Safely extend a lease without claiming another worker's chunk."""
        return self.leases.renew(chunk.chunk_id, self.worker_id)

    # -- status ------------------------------------------------------------

    def progress(self) -> dict:
        return self.leases.progress()

    def active_workers(self) -> dict:
        return self.leases.active_workers()

    def is_complete(self) -> bool:
        return self.progress()["ratio"] >= 1.0

    def merge_results(self) -> Path:
        """Merge all worker event files into a single sorted output."""
        if not self.is_complete():
            raise RuntimeError("Cannot merge an incomplete distributed job")
        return self.store.merge_worker_events(keep_partials=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _plan_chunk_ids(
    start: datetime,
    end: datetime,
    chunk_seconds: float,
) -> list[tuple[str, str, str]]:
    """Return ``(chunk_id, start_iso, end_iso)`` for a fixed-size plan."""
    if chunk_seconds <= 0:
        raise ValueError("chunk_seconds must be > 0")
    chunks: list[tuple[str, str, str]] = []
    cursor = start
    index = 0
    step = timedelta(seconds=chunk_seconds)
    while cursor < end:
        chunk_end = min(cursor + step, end)
        chunk_id = f"{index:06d}-{int(cursor.timestamp())}"
        chunks.append((chunk_id, cursor.isoformat(), chunk_end.isoformat()))
        index += 1
        cursor = chunk_end
    return chunks