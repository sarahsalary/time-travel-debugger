"""Chunk leases backed by a shared JSON file with file-lock atomicity.

A lease is a claim on a time range (chunk) by a specific worker. Leases
have a TTL so that a crashed worker's chunks can be reclaimed by others.
The lease file is a plain JSON document protected by an OS-level file
lock, which works transparently over NFS, SMB, and local filesystems.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

try:
    import portalocker
except ImportError:  # pragma: no cover — fallback for environments without portalocker
    portalocker = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Lease model
# ---------------------------------------------------------------------------


class LeaseStatus(str, Enum):
    """The lifecycle of a chunk lease."""

    PENDING = "pending"
    CLAIMED = "claimed"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class ChunkLease:
    """A lease on one time-range chunk."""

    chunk_id: str
    start: str
    end: str
    status: str = LeaseStatus.PENDING.value
    worker_id: str = ""
    claimed_at: float = 0.0
    expires_at: float = 0.0
    completed_at: float = 0.0
    event_count: int = 0
    output_file: str = ""
    error: str = ""

    @property
    def is_expired(self) -> bool:
        if self.status != LeaseStatus.CLAIMED.value:
            return False
        return time.time() > self.expires_at

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ChunkLease":
        allowed = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in data.items() if k in allowed})


# ---------------------------------------------------------------------------
# File-lock helper
# ---------------------------------------------------------------------------


class _FileLock:
    """Cross-platform file lock with portalocker fallback to fcntl/msvcrt."""

    def __init__(self, path: Path, timeout: float = 30.0) -> None:
        self.path = path
        self.timeout = timeout

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        if portalocker is not None:
            self._handle = open(self.path, "a+")
            portalocker.lock(self._handle, portalocker.LOCK_EX)
        else:
            self._handle = self._fallback_lock()
        return self._handle

    def __exit__(self, *exc_info: Any) -> None:
        if portalocker is not None:
            portalocker.unlock(self._handle)
        else:
            self._fallback_unlock()
        self._handle.close()

    def _fallback_lock(self):
        import sys

        handle = open(self.path, "a+")
        if sys.platform == "win32":
            import msvcrt

            deadline = time.time() + self.timeout
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    return handle
                except OSError:
                    if time.time() > deadline:
                        raise TimeoutError(
                            f"Could not acquire lock {self.path}"
                        )
                    time.sleep(0.05)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            return handle

    def _fallback_unlock(self) -> None:
        import sys

        if sys.platform == "win32":
            import msvcrt

            try:
                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl

            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# Lease manager
# ---------------------------------------------------------------------------


class LeaseManager:
    """Manage chunk leases in a shared JSON file.

    All mutations go through the file lock, so concurrent workers on
    different machines see a consistent view of the lease table.
    """

    def __init__(
        self,
        lease_path: Path,
        *,
        lease_ttl_seconds: float = 300.0,
        lock_timeout: float = 30.0,
    ) -> None:
        self.lease_path = Path(lease_path)
        self.lease_ttl_seconds = lease_ttl_seconds
        self.lock_timeout = lock_timeout
        self._lock_path = self.lease_path.with_suffix(".lock")

    # -- public API --------------------------------------------------------

    def initialize(self, chunks: list[tuple[str, str, str]]) -> None:
        """Write the initial lease table if it does not already exist.

        ``chunks`` is a list of ``(chunk_id, start_iso, end_iso)`` tuples.
        """
        with _FileLock(self._lock_path, self.lock_timeout):
            existing = self._read_unlocked()
            if existing:
                return
            leases = [
                ChunkLease(chunk_id=cid, start=start, end=end)
                for cid, start, end in chunks
            ]
            self._write_unlocked(leases)

    def claim_next(
        self, worker_id: str
    ) -> ChunkLease | None:
        """Atomically claim the next available (or expired) chunk.

        Returns ``None`` if every chunk is already claimed, completed,
        or actively leased by another worker.
        """
        with _FileLock(self._lock_path, self.lock_timeout):
            leases = self._read_unlocked()
            now = time.time()

            # First pass: reclaim expired leases.
            for lease in leases:
                if lease.status == LeaseStatus.CLAIMED.value:
                    if now > lease.expires_at:
                        return self._assign_unlocked(
                            leases, lease, worker_id, now
                        )

            # Second pass: pick a fresh pending chunk.
            for lease in leases:
                if lease.status == LeaseStatus.PENDING.value:
                    return self._assign_unlocked(
                        leases, lease, worker_id, now
                    )

            return None

    def complete(
        self,
        chunk_id: str,
        *,
        event_count: int,
        output_file: str = "",
    ) -> None:
        """Mark a chunk as successfully completed."""
        with _FileLock(self._lock_path, self.lock_timeout):
            leases = self._read_unlocked()
            for lease in leases:
                if lease.chunk_id == chunk_id:
                    lease.status = LeaseStatus.COMPLETED.value
                    lease.completed_at = time.time()
                    lease.event_count = event_count
                    lease.output_file = output_file
                    lease.error = ""
                    break
            self._write_unlocked(leases)

    def fail(self, chunk_id: str, *, error: str = "") -> None:
        """Mark a chunk as failed so it can be retried."""
        with _FileLock(self._lock_path, self.lock_timeout):
            leases = self._read_unlocked()
            for lease in leases:
                if lease.chunk_id == chunk_id:
                    lease.status = LeaseStatus.PENDING.value
                    lease.worker_id = ""
                    lease.error = error
                    break
            self._write_unlocked(leases)

    def renew(self, chunk_id: str, worker_id: str) -> bool:
        """Atomically extend only a lease still owned by this worker."""
        with _FileLock(self._lock_path, self.lock_timeout):
            leases = self._read_unlocked()
            for lease in leases:
                if lease.chunk_id == chunk_id and lease.worker_id == worker_id and lease.status == LeaseStatus.CLAIMED.value:
                    lease.expires_at = time.time() + self.lease_ttl_seconds
                    self._write_unlocked(leases)
                    return True
            return False

    def snapshot(self) -> list[ChunkLease]:
        """Return a read-only copy of the current lease table."""
        with _FileLock(self._lock_path, self.lock_timeout):
            return self._read_unlocked()

    def progress(self) -> dict:
        """Return aggregate progress metrics."""
        leases = self.snapshot()
        total = len(leases)
        completed = sum(
            1 for l in leases if l.status == LeaseStatus.COMPLETED.value
        )
        claimed = sum(
            1 for l in leases if l.status == LeaseStatus.CLAIMED.value
        )
        pending = sum(
            1 for l in leases if l.status == LeaseStatus.PENDING.value
        )
        return {
            "total": total,
            "completed": completed,
            "claimed": claimed,
            "pending": pending,
            "ratio": completed / total if total else 1.0,
        }

    def active_workers(self) -> dict[str, dict]:
        """Return a mapping of worker_id to its active leases."""
        leases = self.snapshot()
        workers: dict[str, dict] = {}
        now = time.time()
        for lease in leases:
            if lease.status != LeaseStatus.CLAIMED.value:
                continue
            if now > lease.expires_at:
                continue
            info = workers.setdefault(
                lease.worker_id,
                {"leases": 0, "chunks": []},
            )
            info["leases"] += 1
            info["chunks"].append(lease.chunk_id)
        return workers

    # -- internals ---------------------------------------------------------

    def _assign_unlocked(
        self,
        leases: list[ChunkLease],
        lease: ChunkLease,
        worker_id: str,
        now: float,
    ) -> ChunkLease:
        lease.status = LeaseStatus.CLAIMED.value
        lease.worker_id = worker_id
        lease.claimed_at = now
        lease.expires_at = now + self.lease_ttl_seconds
        self._write_unlocked(leases)
        return lease

    def _read_unlocked(self) -> list[ChunkLease]:
        if not self.lease_path.exists():
            return []
        try:
            data = json.loads(self.lease_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return [ChunkLease.from_dict(item) for item in data.get("leases", [])]

    def _write_unlocked(self, leases: list[ChunkLease]) -> None:
        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "leases": [lease.to_dict() for lease in leases],
        }
        tmp = self.lease_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(tmp, self.lease_path)