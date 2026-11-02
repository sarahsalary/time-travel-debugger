"""Shared-storage layout for distributed checkpoints.

Everything a distributed job needs lives under a single directory:

    <root>/<job_id>/
        manifest.json     — job identity + chunk plan
        leases.json       — current lease table
        leases.lock       — file lock for leases
        events/           — per-worker event files
        result.jsonl      — merged output (created at the end)
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class SharedJobManifest:
    """Identity and chunk plan for a distributed job."""

    job_id: str
    query: str
    original_start: str
    original_end: str
    mode: str
    total_chunks: int = 0
    chunk_seconds: float = 0.0
    created_at: str = ""
    schema_version: int = 1

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "SharedJobManifest":
        allowed = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in data.items() if k in allowed})


class SharedCheckpointStore:
    """Access the shared-storage layout for one job."""

    def __init__(self, root: Path, job_id: str) -> None:
        self.root = Path(root)
        self.job_id = job_id
        self.job_dir = self.root / job_id
        self.events_dir = self.job_dir / "events"

    # -- paths -------------------------------------------------------------

    @property
    def manifest_path(self) -> Path:
        return self.job_dir / "manifest.json"

    @property
    def lease_path(self) -> Path:
        return self.job_dir / "leases.json"

    @property
    def result_path(self) -> Path:
        return self.job_dir / "result.jsonl"

    def worker_events_path(self, worker_id: str) -> Path:
        return self.events_dir / f"{worker_id}.jsonl"

    # -- manifest ----------------------------------------------------------

    def ensure_directory(self) -> None:
        self.job_dir.mkdir(parents=True, exist_ok=True)
        self.events_dir.mkdir(parents=True, exist_ok=True)

    def write_manifest(self, manifest: SharedJobManifest) -> None:
        self.ensure_directory()
        if not manifest.created_at:
            manifest.created_at = datetime.now(timezone.utc).isoformat()
        tmp = self.manifest_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(manifest.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(tmp, self.manifest_path)

    def read_manifest(self) -> SharedJobManifest | None:
        if not self.manifest_path.exists():
            return None
        try:
            data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return SharedJobManifest.from_dict(data)

    # -- events ------------------------------------------------------------

    def chunk_events_path(self, chunk_id: str) -> Path:
        """One durable output per chunk, independent of worker retries."""
        if not chunk_id or not all(c.isalnum() or c in "-_" for c in chunk_id):
            raise ValueError("Invalid chunk ID")
        return self.events_dir / f"chunk-{chunk_id}.jsonl"

    def save_chunk_events(self, chunk_id: str, lines: list[str]) -> Path:
        """Atomically replace partial chunk results to avoid duplicate replay."""
        self.ensure_directory()
        import uuid
        path = self.chunk_events_path(chunk_id)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with tmp.open("w", encoding="utf-8") as handle:
                for line in lines:
                    handle.write(line.rstrip("\n") + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
        return path

    def append_events(self, worker_id: str, lines: list[str]) -> None:
        """Append JSONL lines to a worker's private event file."""
        self.ensure_directory()
        path = self.worker_events_path(worker_id)
        with path.open("a", encoding="utf-8") as handle:
            for line in lines:
                handle.write(line.rstrip("\n") + "\n")

    def merge_worker_events(
        self,
        *,
        sort: bool = True,
        keep_partials: bool = False,
    ) -> Path:
        """Merge all worker event files into ``result.jsonl``.

        Because workers may have fetched overlapping or interleaved time
        ranges, the merged output is sorted by timestamp by default.
        """
        self.ensure_directory()
        all_lines: list[str] = []
        for path in sorted(self.events_dir.glob("*.jsonl")):
            try:
                with path.open("r", encoding="utf-8") as handle:
                    all_lines.extend(
                        line.rstrip("\n") for line in handle if line.strip()
                    )
            except OSError:
                continue

        if sort:
            all_lines.sort(key=_timestamp_sort_key)

        tmp = self.result_path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            for line in all_lines:
                handle.write(line + "\n")
        os.replace(tmp, self.result_path)

        if not keep_partials:
            for path in self.events_dir.glob("*.jsonl"):
                try:
                    path.unlink()
                except OSError:
                    pass

        return self.result_path


def _timestamp_sort_key(line: str) -> str:
    """Extract a sortable timestamp prefix from a JSONL line.

    Falls back to the empty string so lines without a parseable
    timestamp land at the start rather than crashing the merge.
    """
    try:
        data = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return ""
    ts = data.get("timestamp") or data.get("time") or data.get("ts") or ""
    return str(ts)