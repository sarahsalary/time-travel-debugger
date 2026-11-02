"""Persistent checkpoints for resumable Loki fetches.

A checkpoint records which time ranges have already been fetched so a
run that is interrupted (Ctrl+C, network failure, laptop lid closing)
can be resumed later without re-fetching everything.

Checkpoints live under the XDG data directory (``~/.local/share/ttd/
checkpoints`` by default) and are named after a stable hash of the
query, original time range, and fetch mode. This means the same command
run twice always finds its checkpoint; a different query gets a fresh
one.

The on-disk format is plain JSON so users can inspect, edit, or version
their checkpoints.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Directory helpers
# ---------------------------------------------------------------------------


def default_checkpoint_dir() -> Path:
    """Return the default directory where ttd stores checkpoints."""
    override = os.environ.get("TTD_CHECKPOINT_DIR")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "ttd" / "checkpoints"


def compute_job_id(
    query: str, start: datetime, end: datetime, mode: str
) -> str:
    """Return a stable identifier for a (mode, query, range) tuple."""
    fingerprint = f"{mode}|{query}|{start.isoformat()}|{end.isoformat()}"
    return hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Range arithmetic
# ---------------------------------------------------------------------------


def _merge_ranges(
    ranges: list[tuple[datetime, datetime]]
) -> list[tuple[datetime, datetime]]:
    """Merge overlapping or adjacent ranges into a minimal set."""
    if not ranges:
        return []
    ordered = sorted(ranges, key=lambda r: r[0])
    merged: list[tuple[datetime, datetime]] = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def _compute_gaps(
    start: datetime,
    end: datetime,
    completed: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    """Return the sub-ranges of [start, end] not covered by completed."""
    if start >= end:
        return []
    if not completed:
        return [(start, end)]

    merged = _merge_ranges(completed)
    gaps: list[tuple[datetime, datetime]] = []
    cursor = start
    for c_start, c_end in merged:
        if c_end <= cursor:
            continue
        if c_start > cursor:
            gaps.append((cursor, min(c_start, end)))
        cursor = max(cursor, c_end)
        if cursor >= end:
            return gaps
    if cursor < end:
        gaps.append((cursor, end))
    return gaps


# ---------------------------------------------------------------------------
# Checkpoint state
# ---------------------------------------------------------------------------


@dataclass
class CheckpointState:
    """Serializable state of an in-progress or resumable fetch job."""

    job_id: str
    query: str
    original_start: str
    original_end: str
    mode: str
    config: dict = field(default_factory=dict)
    output_path: str | None = None
    completed_ranges: list[tuple[str, str]] = field(default_factory=list)
    events_fetched: int = 0
    created_at: str = ""
    updated_at: str = ""
    state: str = "in_progress"  # in_progress | completed | aborted

    # -- serialization -----------------------------------------------------

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "CheckpointState":
        allowed = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in data.items() if k in allowed})

    # -- progress ----------------------------------------------------------

    def remaining_gaps(self) -> list[tuple[datetime, datetime]]:
        """Return the sub-ranges still to be fetched."""
        start = datetime.fromisoformat(self.original_start)
        end = datetime.fromisoformat(self.original_end)
        completed = [
            (datetime.fromisoformat(s), datetime.fromisoformat(e))
            for s, e in self.completed_ranges
        ]
        return _compute_gaps(start, end, completed)

    @property
    def progress_ratio(self) -> float:
        """Return a 0.0–1.0 estimate of how much of the range is done."""
        start = datetime.fromisoformat(self.original_start)
        end = datetime.fromisoformat(self.original_end)
        total = (end - start).total_seconds()
        if total <= 0:
            return 1.0
        remaining = sum(
            (e - s).total_seconds() for s, e in self.remaining_gaps()
        )
        return max(0.0, min(1.0, 1.0 - remaining / total))


# ---------------------------------------------------------------------------
# Persistent store
# ---------------------------------------------------------------------------


class CheckpointStore:
    """Load, save, and enumerate checkpoints on disk."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root else default_checkpoint_dir()
        self._lock = threading.RLock()

    def path_for(self, job_id: str) -> Path:
        return self.root / f"{job_id}.json"

    def exists(self, job_id: str) -> bool:
        return self.path_for(job_id).exists()

    def load(self, job_id: str) -> CheckpointState | None:
        path = self.path_for(job_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return CheckpointState.from_dict(data)

    def save(self, state: CheckpointState) -> Path:
        """Write the checkpoint atomically (write to tmp, then rename)."""
        self.root.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc).isoformat()
        if not state.created_at:
            state.created_at = now
        state.updated_at = now

        path = self.path_for(state.job_id)
        tmp = path.with_suffix(".tmp")
        with self._lock:
            tmp.write_text(
                json.dumps(state.to_dict(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(tmp, path)
        return path

    def delete(self, job_id: str) -> bool:
        path = self.path_for(job_id)
        if not path.exists():
            return False
        with self._lock:
            path.unlink()
        return True

    def list_all(self) -> list[CheckpointState]:
        if not self.root.exists():
            return []
        states: list[CheckpointState] = []
        for path in self.root.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                states.append(CheckpointState.from_dict(data))
            except (OSError, json.JSONDecodeError):
                continue
        states.sort(key=lambda s: s.updated_at, reverse=True)
        return states

    def clear(self) -> int:
        if not self.root.exists():
            return 0
        removed = 0
        with self._lock:
            for path in self.root.glob("*.json"):
                path.unlink()
                removed += 1
        return removed


# ---------------------------------------------------------------------------
# Live tracker
# ---------------------------------------------------------------------------


class CheckpointTracker:
    """Thread-safe tracker that mutates a checkpoint as workers progress.

    Workers call ``record_chunk_complete`` after each successful chunk;
    the tracker updates the checkpoint in memory and periodically writes
    it to disk. When the process is interrupted, ``mark_aborted``
    persists the final state so the next run can resume from it.
    """

    def __init__(
        self,
        store: CheckpointStore,
        state: CheckpointState,
        *,
        autosave_interval: float = 5.0,
    ) -> None:
        self.store = store
        self.state = state
        self.autosave_interval = autosave_interval
        self._lock = threading.RLock()
        self._last_save = 0.0

    def record_chunk_complete(
        self, start: datetime, end: datetime, event_count: int
    ) -> None:
        with self._lock:
            self.state.completed_ranges.append(
                (start.isoformat(), end.isoformat())
            )
            self.state.events_fetched += event_count
            self._maybe_autosave()

    def flush(self) -> Path:
        """Force a synchronous save."""
        with self._lock:
            return self.store.save(self.state)

    def mark_complete(self) -> None:
        with self._lock:
            self.state.state = "completed"
            self.store.save(self.state)

    def mark_aborted(self) -> None:
        with self._lock:
            self.state.state = "aborted"
            self.store.save(self.state)

    def _maybe_autosave(self) -> None:
        now = time.monotonic()
        if now - self._last_save < self.autosave_interval:
            return
        self._last_save = now
        try:
            self.store.save(self.state)
        except OSError:
            pass