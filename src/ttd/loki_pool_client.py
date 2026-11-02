"""A drop-in replacement for ``LokiClient`` backed by a pool of nodes.

``LokiPoolClient`` presents the same public API as ``LokiClient`` so the
rest of ``ttd`` — adaptive chunking, reslicing, checkpointing — works
unchanged. Internally it routes each call through the pool, so the
caller benefits from load balancing, failover, and per-node circuit
breakers without any changes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from .loki import LokiClient
from .loki_pool import (
    BalancingStrategy,
    LokiNodeConfig,
    LokiPool,
    PoolNode,
)


class LokiPoolClient:
    """A ``LokiClient``-compatible façade over a ``LokiPool``."""

    def __init__(
        self,
        nodes: list[LokiNodeConfig],
        *,
        strategy: BalancingStrategy = BalancingStrategy.WEIGHTED_ROUND_ROBIN,
        **pool_kwargs: Any,
    ) -> None:
        self.pool = LokiPool(nodes, strategy=strategy, **pool_kwargs)
        # Expose the first node's client for API compatibility with code
        # that inspects ``_retry_handler`` or ``_circuit`` directly.
        self._retry_handler = self.pool.nodes[0].client._retry_handler
        self._circuit = self.pool.nodes[0].client._circuit

    # -- properties --------------------------------------------------------

    @property
    def nodes(self) -> list[PoolNode]:
        return self.pool.nodes

    @property
    def healthy_nodes(self) -> list[PoolNode]:
        return self.pool.healthy_nodes

    @property
    def circuit_state(self) -> str:
        """Aggregate circuit state: the worst state across all nodes."""
        states = [n.client.circuit_state for n in self.pool.nodes]
        if all(s == "disabled" for s in states):
            return "disabled"
        if any(s == "closed" for s in states):
            return "closed"
        if any(s == "half_open" for s in states):
            return "half_open"
        return "open"

    # -- delegation --------------------------------------------------------

    def query_range(
        self,
        logql: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 1000,
        direction: str = "forward",
    ):
        return self.pool.call(
            lambda client: client.query_range(
                logql,
                start=start,
                end=end,
                limit=limit,
                direction=direction,
            ),
            key=logql,
        )

    def query_instant(
        self,
        logql: str,
        *,
        at: datetime | None = None,
        limit: int = 1000,
    ):
        return self.pool.call(
            lambda client: client.query_instant(
                logql, at=at, limit=limit
            ),
            key=logql,
        )

    def stream_range(self, *args: Any, **kwargs: Any):
        return self.pool.call(
            lambda client: list(client.stream_range(*args, **kwargs)),
            key=kwargs.get("logql") or (args[0] if args else ""),
        )

    def parallel_stream_range(self, *args: Any, **kwargs: Any):
        return self.pool.call(
            lambda client: list(
                client.parallel_stream_range(*args, **kwargs)
            ),
            key=kwargs.get("logql") or (args[0] if args else ""),
        )

    def adaptive_stream_range(self, *args: Any, **kwargs: Any):
        return self.pool.call(
            lambda client: list(
                client.adaptive_stream_range(*args, **kwargs)
            ),
            key=kwargs.get("logql") or (args[0] if args else ""),
        )

    def labels(self):
        # Labels are replicated across nodes; pick the first healthy one.
        return self.pool.call(lambda client: client.labels())

    def label_values(self, label: str):
        return self.pool.call(
            lambda client: client.label_values(label)
        )

    # -- admin -------------------------------------------------------------

    def snapshot(self) -> list[dict]:
        return self.pool.snapshot()

    def reset_all_circuits(self) -> None:
        for node in self.pool.nodes:
            node.client.reset_circuit()
            node.mark_healthy()

    def mark_all_healthy(self) -> None:
        for node in self.pool.nodes:
            node.mark_healthy()