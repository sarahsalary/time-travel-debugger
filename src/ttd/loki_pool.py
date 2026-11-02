"""Multi-instance Loki pool with weighted load balancing.

The pool manages a set of ``LokiClient`` instances, each with its own
circuit breaker, health status, and weight. A routing strategy picks
the best node for each request, skipping unhealthy nodes and nodes whose
circuit is open.

Two modes of operation are supported:

* **Routing mode** — each request is routed to a single node. This is
  what you want when nodes hold *replicated* data (HA replicas).

* **Fanout mode** — each request is sent to *all* healthy nodes and the
  results are merged. This is what you want when nodes hold
  *partitioned* data (different tenants, different regions).

The pool also supports **consistent hashing** for fanout mode, so that
the same time range always goes to the same node, which keeps the
checkpoint and reslicing logic stable across runs.
"""

from __future__ import annotations

import hashlib
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Callable, Iterator

from .loki import LokiClient, LokiError
from .loki_circuit import (
    CircuitConfig,
    CircuitOpenError,
    CircuitState,
    StateTransition,
)
from .loki_retry import RetryPolicy


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


class BalancingStrategy(str, Enum):
    """How to pick a node from the pool."""

    WEIGHTED_ROUND_ROBIN = "weighted_round_robin"
    LEAST_CONNECTIONS = "least_connections"
    LEAST_RESPONSE_TIME = "least_response_time"
    RANDOM_WEIGHTED = "random_weighted"


# ---------------------------------------------------------------------------
# Node configuration
# ---------------------------------------------------------------------------


@dataclass
class LokiNodeConfig:
    """Configuration for one Loki node in the pool."""

    url: str
    name: str = ""
    weight: int = 1
    tenant: str | None = None
    username: str | None = None
    password: str | None = None
    timeout: float = 30.0

    #: Optional tags for filtering (e.g. {"region": "us-east"}).
    tags: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name:
            # Derive a stable name from the URL.
            self.name = hashlib.sha1(self.url.encode()).hexdigest()[:8]
        if self.weight < 1:
            raise ValueError(f"weight must be >= 1 (got {self.weight})")


# ---------------------------------------------------------------------------
# Health record
# ---------------------------------------------------------------------------


@dataclass
class NodeHealth:
    """Runtime health state for one node in the pool."""

    total_requests: int = 0
    total_failures: int = 0
    consecutive_failures: int = 0
    active_requests: int = 0
    last_latency: float = 0.0
    avg_latency: float = 0.0
    last_success_at: float = 0.0
    last_failure_at: float = 0.0
    is_healthy: bool = True

    @property
    def success_rate(self) -> float:
        if self.total_requests == 0:
            return 1.0
        return 1.0 - (self.total_failures / self.total_requests)

    def record_success(self, latency: float) -> None:
        self.total_requests += 1
        self.consecutive_failures = 0
        self.last_latency = latency
        self.last_success_at = time.monotonic()
        # Exponential moving average of latency.
        alpha = 0.3
        self.avg_latency = (
            alpha * latency + (1 - alpha) * self.avg_latency
            if self.avg_latency
            else latency
        )

    def record_failure(self) -> None:
        self.total_requests += 1
        self.total_failures += 1
        self.consecutive_failures += 1
        self.last_failure_at = time.monotonic()

    def snapshot(self) -> dict:
        return {
            "total_requests": self.total_requests,
            "total_failures": self.total_failures,
            "consecutive_failures": self.consecutive_failures,
            "active_requests": self.active_requests,
            "success_rate": round(self.success_rate, 3),
            "avg_latency_ms": round(self.avg_latency * 1000, 1),
            "is_healthy": self.is_healthy,
        }


# ---------------------------------------------------------------------------
# Pool node
# ---------------------------------------------------------------------------


class PoolNode:
    """A single node in the pool, wrapping a ``LokiClient``."""

    def __init__(
        self,
        config: LokiNodeConfig,
        client: LokiClient,
        *,
        health_threshold: int = 3,
    ) -> None:
        self.config = config
        self.client = client
        self.health = NodeHealth()
        self.health_threshold = health_threshold
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def weight(self) -> int:
        return self.config.weight

    def is_available(self) -> bool:
        """Return True if the node can serve requests."""
        if not self.health.is_healthy:
            return False
        state = self.client.circuit_state
        if state == "open":
            return False
        return True

    def acquire(self) -> None:
        with self._lock:
            self.health.active_requests += 1

    def release(self, *, success: bool, latency: float) -> None:
        with self._lock:
            self.health.active_requests = max(0, self.health.active_requests - 1)
            if success:
                self.health.record_success(latency)
            else:
                self.health.record_failure()
                if (
                    self.health.consecutive_failures
                    >= self.health_threshold
                ):
                    self.health.is_healthy = False

    def mark_healthy(self) -> None:
        with self._lock:
            self.health.is_healthy = True
            self.health.consecutive_failures = 0

    def snapshot(self) -> dict:
        return {
            "name": self.name,
            "url": self.config.url,
            "weight": self.weight,
            "tenant": self.config.tenant,
            "tags": self.config.tags,
            "circuit": self.client.circuit_state,
            **self.health.snapshot(),
        }


# ---------------------------------------------------------------------------
# Pool
# ---------------------------------------------------------------------------


class LokiPool:
    """A pool of Loki nodes with health tracking and load balancing."""

    def __init__(
        self,
        nodes: list[LokiNodeConfig],
        *,
        strategy: BalancingStrategy = BalancingStrategy.WEIGHTED_ROUND_ROBIN,
        retry_policy: RetryPolicy | None = None,
        circuit_config: CircuitConfig | None = None,
        on_circuit_change: Callable[[StateTransition], None] | None = None,
        health_threshold: int = 3,
    ) -> None:
        if not nodes:
            raise ValueError("LokiPool requires at least one node")

        self.strategy = strategy
        self._nodes: list[PoolNode] = []
        self._lock = threading.RLock()
        self._rr_counter = 0
        self._rr_weights: list[int] = []
        self._health_threshold = health_threshold

        for config in nodes:
            client = LokiClient(
                url=config.url,
                tenant=config.tenant,
                username=config.username,
                password=config.password,
                timeout=config.timeout,
                retry_policy=retry_policy or RetryPolicy(),
                circuit_config=circuit_config,
                on_circuit_change=on_circuit_change,
            )
            self._nodes.append(
                PoolNode(
                    config,
                    client,
                    health_threshold=health_threshold,
                )
            )
            self._rr_weights.append(config.weight)

        self._rr_sequence = _build_rr_sequence(self._rr_weights)

    # -- public API --------------------------------------------------------

    @property
    def nodes(self) -> list[PoolNode]:
        return list(self._nodes)

    @property
    def healthy_nodes(self) -> list[PoolNode]:
        return [n for n in self._nodes if n.is_available()]

    def snapshot(self) -> list[dict]:
        return [n.snapshot() for n in self._nodes]

    def select(self) -> PoolNode:
        """Pick the next node according to the strategy.

        Raises ``LokiError`` if no node is available.
        """
        with self._lock:
            available = [n for n in self._nodes if n.is_available()]
            if not available:
                # No healthy node: try to revive any node whose circuit
                # has since moved to HALF_OPEN.
                for node in self._nodes:
                    if node.client.circuit_state == "half_open":
                        node.mark_healthy()
                        available.append(node)
                if not available:
                    raise LokiError(
                        "No healthy Loki nodes available in the pool"
                    )

            if self.strategy is BalancingStrategy.WEIGHTED_ROUND_ROBIN:
                return self._select_wrr(available)
            if self.strategy is BalancingStrategy.LEAST_CONNECTIONS:
                return min(
                    available,
                    key=lambda n: n.health.active_requests,
                )
            if self.strategy is BalancingStrategy.LEAST_RESPONSE_TIME:
                return min(
                    available,
                    key=lambda n: (
                        n.health.avg_latency or float("inf")
                    ),
                )
            if self.strategy is BalancingStrategy.RANDOM_WEIGHTED:
                return self._select_random_weighted(available)
            return available[0]

    def select_for_key(self, key: str) -> PoolNode:
        """Pick a node deterministically based on ``key``.

        Used by fanout mode with consistent hashing: the same key always
        maps to the same node as long as the pool does not change.
        """
        with self._lock:
            available = [n for n in self._nodes if n.is_available()]
            if not available:
                raise LokiError(
                    "No healthy Loki nodes available in the pool"
                )
            # Hash the key and map it to a node by weight.
            digest = hashlib.md5(key.encode()).digest()
            value = int.from_bytes(digest[:4], "big")
            total_weight = sum(n.weight for n in available)
            if total_weight == 0:
                return available[0]
            target = value % total_weight
            cumulative = 0
            for node in available:
                cumulative += node.weight
                if target < cumulative:
                    return node
            return available[-1]

    def call(
        self,
        fn: Callable[[LokiClient], object],
        *,
        key: str | None = None,
    ) -> object:
        """Execute ``fn(client)`` on the best available node.

        ``fn`` receives the selected ``LokiClient``. If it raises, the
        failure is recorded for that node and the exception propagates.
        """
        node = self.select_for_key(key) if key else self.select()
        node.acquire()
        start = time.monotonic()
        try:
            result = fn(node.client)
        except Exception:
            latency = time.monotonic() - start
            node.release(success=False, latency=latency)
            raise
        latency = time.monotonic() - start
        node.release(success=True, latency=latency)
        return result

    def fanout(
        self,
        fn: Callable[[LokiClient], object],
        *,
        merge: Callable[[list[object]], object] | None = None,
    ) -> object:
        """Execute ``fn`` on every healthy node and merge the results."""
        available = self.healthy_nodes
        if not available:
            raise LokiError(
                "No healthy Loki nodes available for fanout"
            )

        results: list[object] = []
        errors: list[tuple[str, Exception]] = []
        for node in available:
            node.acquire()
            start = time.monotonic()
            try:
                results.append(fn(node.client))
                node.release(
                    success=True, latency=time.monotonic() - start
                )
            except Exception as exc:
                node.release(
                    success=False, latency=time.monotonic() - start
                )
                errors.append((node.name, exc))

        if not results and errors:
            raise LokiError(
                "All nodes failed during fanout: "
                + "; ".join(f"{n}: {e}" for n, e in errors)
            )

        if merge is not None:
            return merge(results)
        return results

    # -- internals ---------------------------------------------------------

    def _select_wrr(self, available: list[PoolNode]) -> PoolNode:
        """Smooth weighted round-robin (Nginx-style)."""
        # Build a sequence of node names honouring weights. We rebuild
        # the sequence whenever the available set changes.
        names = [n.name for n in available]
        weights = {n.name: n.weight for n in available}
        by_name = {n.name: n for n in available}

        # Build a static sequence once and rotate through it.
        sequence: list[str] = []
        for name in names:
            sequence.extend([name] * weights[name])
        if not sequence:
            return available[0]

        idx = self._rr_counter % len(sequence)
        self._rr_counter += 1
        return by_name[sequence[idx]]

    def _select_random_weighted(
        self, available: list[PoolNode]
    ) -> PoolNode:
        total = sum(n.weight for n in available)
        if total <= 0:
            return random.choice(available)
        pick = random.uniform(0, total)
        cumulative = 0.0
        for node in available:
            cumulative += node.weight
            if pick <= cumulative:
                return node
        return available[-1]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_rr_sequence(weights: list[int]) -> list[int]:
    """Expand weights into a round-robin sequence of node indices."""
    sequence: list[int] = []
    for index, weight in enumerate(weights):
        sequence.extend([index] * weight)
    return sequence