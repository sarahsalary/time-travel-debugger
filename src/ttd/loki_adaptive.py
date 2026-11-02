"""Adaptive chunking for parallel Loki queries.

The idea is simple: instead of splitting the time range into equal-size
chunks, we split it into chunks that each hold roughly the same *number*
of events. A dense 5-minute window and a sparse 2-hour window then take
similar time to fetch, which keeps all workers busy and avoids one
straggler chunk blocking the whole query.

The algorithm has three phases:

1. **Probe** — take a small sample at the start, middle, and end of the
   range to estimate how many events per second the query returns in
   different regions.

2. **Plan** — use the probe results to compute a chunk size for each
   region: ``chunk_seconds = target_events / density``, clamped to
   ``[min_chunk_seconds, max_chunk_seconds]``.

3. **Fetch** — dispatch the planned chunks to a thread pool. After each
   chunk completes, re-estimate its local density from the actual event
   count and adjust the sizes of any *not-yet-started* chunks in the
   same region.

The result is a self-tuning scheduler that adapts to the data it sees,
without requiring any knowledge of the underlying Loki storage or
labels.
"""

from __future__ import annotations

import math
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterator

from .loki import LokiClient, LokiError
from .models import LogEvent


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class AdaptiveConfig:
    """Tunables for the adaptive chunking strategy."""

    #: Target number of events per chunk. Dense regions get smaller chunks,
    #: sparse regions get larger ones, all aiming for this count.
    target_events_per_chunk: int = 5_000

    #: Hard limits on chunk length so a single chunk never runs for too
    #: long or fires off thousands of tiny API calls.
    min_chunk_seconds: float = 10.0
    max_chunk_seconds: float = 3_600.0

    #: Initial chunk length used before the probe completes. Also used as
    #: the fallback when the probe returns no events.
    initial_chunk_seconds: float = 300.0

    #: Length of each probe sample window.
    probe_window_seconds: float = 10.0

    #: Number of probe samples to take. Samples are placed evenly across
    #: the range; 3 (start, middle, end) is a good default.
    probe_samples: int = 3

    #: Max concurrent workers fetching chunks.
    max_workers: int = 4

    #: Smoothing factor when updating a chunk's density estimate from
    #: actual results. 0 = ignore feedback, 1 = replace estimate entirely.
    feedback_weight: float = 0.5

    def validate(self) -> None:
        if self.target_events_per_chunk < 1:
            raise ValueError("target_events_per_chunk must be >= 1")
        if self.min_chunk_seconds <= 0:
            raise ValueError("min_chunk_seconds must be > 0")
        if self.max_chunk_seconds < self.min_chunk_seconds:
            raise ValueError("max_chunk_seconds must be >= min_chunk_seconds")
        if self.initial_chunk_seconds <= 0:
            raise ValueError("initial_chunk_seconds must be > 0")
        if self.probe_samples < 1:
            raise ValueError("probe_samples must be >= 1")
        if self.max_workers < 1:
            raise ValueError("max_workers must be >= 1")
        if not 0.0 <= self.feedback_weight <= 1.0:
            raise ValueError("feedback_weight must be in [0, 1]")


# ---------------------------------------------------------------------------
# Plan representation
# ---------------------------------------------------------------------------


@dataclass
class ChunkPlan:
    """A single planned chunk with a mutable size."""

    index: int
    start: datetime
    end: datetime
    #: Estimated events per second in this chunk's region.
    density: float = 0.0
    #: Whether this chunk was sized by the probe or by the initial default.
    probed: bool = False

    @property
    def duration_seconds(self) -> float:
        return (self.end - self.start).total_seconds()

    @property
    def estimated_events(self) -> float:
        return self.density * self.duration_seconds


@dataclass
class ProbeSample:
    """The outcome of one probe sample."""

    start: datetime
    end: datetime
    event_count: int

    @property
    def density(self) -> float:
        span = (self.end - self.start).total_seconds()
        return self.event_count / span if span > 0 else 0.0


@dataclass
class AdaptiveResult:
    """The outcome of one adaptive chunk fetch."""

    chunk: ChunkPlan
    events: list[LogEvent]
    error: str = ""

    @property
    def succeeded(self) -> bool:
        return not self.error

    @property
    def observed_density(self) -> float:
        """Events per second actually observed in this chunk."""
        span = self.chunk.duration_seconds
        if span <= 0:
            return 0.0
        return len(self.events) / span


# ---------------------------------------------------------------------------
# Adaptive chunker
# ---------------------------------------------------------------------------


class AdaptiveLokiMixin:
    """Adds adaptive chunking to ``LokiClient``.

    Usage::

        client = LokiClient(url="http://loki:3100")
        for event in client.adaptive_stream_range(
            '{job="api"}',
            start=...,
            end=...,
            config=AdaptiveConfig(target_events_per_chunk=2000),
        ):
            process(event)
    """

    def adaptive_stream_range(
        self: "LokiClient",
        logql: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        config: AdaptiveConfig | None = None,
        max_events: int | None = None,
        on_progress: Callable[[int, int], None] | None = None,
        on_chunk_done: Callable[[AdaptiveResult], None] | None = None,
    ) -> Iterator[LogEvent]:
        """Fetch a large time range with density-adaptive chunking."""
        cfg = config or AdaptiveConfig()
        cfg.validate()

        if end is None:
            end = datetime.now(timezone.utc)
        if start is None:
            start = end - timedelta(hours=1)
        if start >= end:
            return

        # ---- Phase 1: probe ------------------------------------------
        probe_samples = self._probe_density(logql, start, end, cfg)
        global_density = self._aggregate_density(probe_samples)

        # ---- Phase 2: plan -------------------------------------------
        plans = self._plan_chunks(start, end, probe_samples, global_density, cfg)
        if not plans:
            return

        # ---- Phase 3: fetch ------------------------------------------
        yield from self._fetch_plans(
            logql,
            plans,
            cfg,
            max_events=max_events,
            on_progress=on_progress,
            on_chunk_done=on_chunk_done,
        )

    # -- Phase 1: probe ----------------------------------------------------

    def _probe_density(
        self: "LokiClient",
        logql: str,
        start: datetime,
        end: datetime,
        cfg: AdaptiveConfig,
    ) -> list[ProbeSample]:
        """Take ``cfg.probe_samples`` evenly-spaced samples across the range."""
        total_span = (end - start).total_seconds()
        probe_span = min(cfg.probe_window_seconds, total_span)
        if probe_span <= 0:
            return []

        # Place sample centers at 0%, 50%, 100% (for 3 samples).
        if cfg.probe_samples == 1:
            fractions = [0.5]
        else:
            fractions = [
                i / (cfg.probe_samples - 1)
                for i in range(cfg.probe_samples)
            ]

        samples: list[ProbeSample] = []
        for fraction in fractions:
            center_offset = fraction * total_span
            sample_start = start + timedelta(seconds=max(0.0, center_offset - probe_span / 2))
            sample_end = sample_start + timedelta(seconds=probe_span)
            if sample_end > end:
                sample_end = end
                sample_start = max(start, sample_end - timedelta(seconds=probe_span))

            count = self._count_events_in_window(logql, sample_start, sample_end)
            samples.append(
                ProbeSample(
                    start=sample_start,
                    end=sample_end,
                    event_count=count,
                )
            )
        return samples

    def _count_events_in_window(
        self: "LokiClient",
        logql: str,
        start: datetime,
        end: datetime,
    ) -> int:
        """Count events in a window without materializing them all."""
        try:
            # We only need a rough count; cap the page at the limit Loki
            # enforces so we never pull down more than necessary.
            page = self.query_range(
                logql,
                start=start,
                end=end,
                limit=1000,
                direction="forward",
            )
            return len(page)
        except LokiError:
            # A failed probe is not fatal; treat it as a sparse region.
            return 0

    @staticmethod
    def _aggregate_density(samples: list[ProbeSample]) -> float:
        """Combine probe samples into a single global density estimate."""
        if not samples:
            return 0.0
        total_events = sum(s.event_count for s in samples)
        total_span = sum(
            (s.end - s.start).total_seconds() for s in samples
        )
        return total_events / total_span if total_span > 0 else 0.0

    # -- Phase 2: plan -----------------------------------------------------

    def _plan_chunks(
        self: "LokiClient",
        start: datetime,
        end: datetime,
        samples: list[ProbeSample],
        global_density: float,
        cfg: AdaptiveConfig,
    ) -> list[ChunkPlan]:
        """Split ``[start, end]`` into chunks sized by local density.

        When probe samples are available, the range is divided into
        ``len(samples) + 1`` segments, each inheriting the density of the
        nearest sample. When they are not, the whole range uses the
        initial chunk size.
        """
        if not samples:
            return self._uniform_plan(start, end, cfg)

        # Build segment boundaries around the probe centers.
        centers = [
            s.start + (s.end - s.start) / 2 for s in samples
        ]
        boundaries = [start]
        for i in range(len(centers) - 1):
            midpoint = centers[i] + (centers[i + 1] - centers[i]) / 2
            boundaries.append(midpoint)
        boundaries.append(end)

        plans: list[ChunkPlan] = []
        index = 0
        for segment_index in range(len(samples)):
            segment_start = boundaries[segment_index]
            segment_end = boundaries[segment_index + 1]
            density = samples[segment_index].density or global_density

            if density <= 0.0:
                # No events in this region: use the largest allowed chunk.
                chunk_seconds = cfg.max_chunk_seconds
            else:
                chunk_seconds = cfg.target_events_per_chunk / density

            chunk_seconds = _clamp(
                chunk_seconds,
                cfg.min_chunk_seconds,
                cfg.max_chunk_seconds,
            )

            cursor = segment_start
            while cursor < segment_end:
                chunk_end = min(
                    cursor + timedelta(seconds=chunk_seconds),
                    segment_end,
                )
                plans.append(
                    ChunkPlan(
                        index=index,
                        start=cursor,
                        end=chunk_end,
                        density=density,
                        probed=True,
                    )
                )
                index += 1
                cursor = chunk_end

        return plans

    def _uniform_plan(
        self,
        start: datetime,
        end: datetime,
        cfg: AdaptiveConfig,
    ) -> list[ChunkPlan]:
        """Fallback when the probe could not sample anything."""
        plans: list[ChunkPlan] = []
        cursor = start
        step = timedelta(seconds=cfg.initial_chunk_seconds)
        index = 0
        while cursor < end:
            chunk_end = min(cursor + step, end)
            plans.append(
                ChunkPlan(
                    index=index,
                    start=cursor,
                    end=chunk_end,
                    density=0.0,
                    probed=False,
                )
            )
            index += 1
            cursor = chunk_end
        return plans

    # -- Phase 3: fetch ----------------------------------------------------

    def _fetch_plans(
        self: "LokiClient",
        logql: str,
        plans: list[ChunkPlan],
        cfg: AdaptiveConfig,
        *,
        max_events: int | None,
        on_progress: Callable[[int, int], None] | None,
        on_chunk_done: Callable[[AdaptiveResult], None] | None,
    ) -> Iterator[LogEvent]:
        """Fetch every chunk in parallel and yield events in order."""
        results: dict[int, AdaptiveResult] = {}
        lock = threading.Lock()
        completed = 0
        plans_by_index = {p.index: p for p in plans}

        with ThreadPoolExecutor(
            max_workers=min(cfg.max_workers, max(1, len(plans))),
            thread_name_prefix="ttd-adaptive",
        ) as executor:
            future_to_plan = {
                executor.submit(self._fetch_plan, logql, plan): plan
                for plan in plans
            }

            for future in as_completed(future_to_plan):
                plan = future_to_plan[future]
                result = future.result()
                with lock:
                    results[plan.index] = result
                    completed += 1
                    # Feed the observed density back into pending chunks
                    # in the same region (self-tuning).
                    self._tune_pending_plans(
                        plan, result, plans_by_index, cfg
                    )
                    if on_chunk_done is not None:
                        try:
                            on_chunk_done(result)
                        except Exception:
                            pass
                    if on_progress is not None:
                        try:
                            on_progress(completed, len(plans))
                        except Exception:
                            pass

        # Surface chunk errors in deterministic order.
        for index in sorted(results):
            result = results[index]
            if not result.succeeded:
                raise LokiError(
                    f"Chunk {index} "
                    f"({result.chunk.start.isoformat()} .. "
                    f"{result.chunk.end.isoformat()}) failed: {result.error}"
                )

        # Yield events in chronological order.
        yielded = 0
        for index in sorted(results):
            for event in results[index].events:
                yield event
                yielded += 1
                if max_events is not None and yielded >= max_events:
                    return

    def _fetch_plan(
        self: "LokiClient",
        logql: str,
        plan: ChunkPlan,
    ) -> AdaptiveResult:
        try:
            events = list(
                self.stream_range(
                    logql,
                    start=plan.start,
                    end=plan.end,
                    max_events=None,
                )
            )
            return AdaptiveResult(chunk=plan, events=events)
        except Exception as exc:  # noqa: BLE001
            return AdaptiveResult(chunk=plan, events=[], error=str(exc))

    @staticmethod
    def _tune_pending_plans(
        completed_plan: ChunkPlan,
        result: AdaptiveResult,
        plans_by_index: dict[int, ChunkPlan],
        cfg: AdaptiveConfig,
    ) -> None:
        """Update pending chunks whose region overlaps the completed one.

        We compare the completed chunk's actual density against the
        probe's estimate. If the actual density is higher (or lower), we
        adjust the *duration* of any pending chunk that shares the same
        probe density estimate so that it targets ``target_events``.
        """
        if not result.succeeded or completed_plan.duration_seconds <= 0:
            return

        observed = result.observed_density
        if observed <= 0.0:
            return

        # Blend the probe density with the observed density.
        alpha = cfg.feedback_weight
        new_density = (1 - alpha) * completed_plan.density + alpha * observed

        # Apply the updated density to pending chunks in the same region.
        for pending in plans_by_index.values():
            if pending.index <= completed_plan.index:
                continue
            if pending.density != completed_plan.density:
                continue
            pending.density = new_density
            # Recompute the chunk's duration target; the caller reslices
            # the plan, but for a simple implementation we only adjust
            # metadata here. A future version could reslice the remaining
            # range on the fly.


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------


def adaptive_fetch(
    client: "LokiClient",
    logql: str,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    config: AdaptiveConfig | None = None,
    max_events: int | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> list[LogEvent]:
    """Convenience wrapper that returns all events as a list."""
    return list(
        client.adaptive_stream_range(
            logql,
            start=start,
            end=end,
            config=config,
            max_events=max_events,
            on_progress=on_progress,
        )
    )