"""Drain-inspired log template miner for clustering similar events.

This module implements a compact version of the Drain algorithm: log
messages are preprocessed to mask variable parts (IPs, UUIDs, numbers,
hex literals, ...), tokenized, and inserted into a fixed-depth parse tree.
At the leaf level, the incoming message is matched against existing
clusters using a token-similarity metric; if the best match exceeds the
configured threshold, the message joins that cluster, otherwise a new
cluster is created.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from .models import LogEvent


# ---------------------------------------------------------------------------
# Preprocessing
# ---------------------------------------------------------------------------

# Ordered from most specific to least specific so that e.g. a UUID is not
# partially consumed by the generic number rule.
_MASK_RULES: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
            r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
        ),
        "<UUID>",
    ),
    (
        re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"),
        "<IP>",
    ),
    (
        re.compile(r"\b(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\b"),
        "<MAC>",
    ),
    (
        re.compile(r"\b0x[0-9a-fA-F]+\b"),
        "<HEX>",
    ),
    (
        re.compile(r"\b[0-9a-fA-F]{16,}\b"),
        "<HASH>",
    ),
    (
        re.compile(
            r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"
            r"(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"
        ),
        "<TIMESTAMP>",
    ),
    (
        re.compile(r"\b\d+(?:\.\d+)?(?:ms|s|m|h|MB|GB|KB|B)\b"),
        "<DURATION>",
    ),
    (
        re.compile(r"\b\d+(?:\.\d+)?\b"),
        "<NUM>",
    ),
    (
        re.compile(r"'(?:[^'\\]|\\.)*'"),
        "<STR>",
    ),
    (
        re.compile(r'"(?:[^"\\]|\\.)*"'),
        "<STR>",
    ),
    (
        re.compile(r"\b(?:/[^\s]+)+"),
        "<PATH>",
    ),
]


def mask_variables(message: str) -> str:
    """Replace variable parts of a message with typed placeholders."""
    result = message
    for pattern, placeholder in _MASK_RULES:
        result = pattern.sub(placeholder, result)
    return result.strip()


def tokenize(message: str) -> list[str]:
    """Split a preprocessed message into whitespace-delimited tokens."""
    return message.split()


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class TemplateCluster:
    """A group of log events sharing the same message template."""

    cluster_id: int
    template_tokens: list[str]
    count: int = 0
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    level_counts: Counter[str] = field(default_factory=Counter)
    service_counts: Counter[str] = field(default_factory=Counter)
    sample_event: LogEvent | None = None

    @property
    def template(self) -> str:
        """The template string with ``<*>`` for variable positions."""
        return " ".join(self.template_tokens)

    @property
    def is_error(self) -> bool:
        return any(
            self.level_counts.get(lvl, 0)
            for lvl in ("ERROR", "CRITICAL", "FATAL")
        )

    @property
    def error_count(self) -> int:
        return sum(
            self.level_counts.get(lvl, 0)
            for lvl in ("ERROR", "CRITICAL", "FATAL")
        )

    @property
    def severity_rank(self) -> int:
        """Higher value means more severe."""
        if self.level_counts.get("CRITICAL") or self.level_counts.get("FATAL"):
            return 4
        if self.level_counts.get("ERROR"):
            return 3
        if self.level_counts.get("WARNING") or self.level_counts.get("WARN"):
            return 2
        if self.level_counts.get("INFO"):
            return 1
        return 0


@dataclass
class ClusterReport:
    """The full result of running the miner over a set of events."""

    clusters: list[TemplateCluster]
    total_events: int
    total_clusters: int

    @property
    def unique_ratio(self) -> float:
        if self.total_events == 0:
            return 0.0
        return self.total_clusters / self.total_events

    def top(self, n: int = 20) -> list[TemplateCluster]:
        return sorted(
            self.clusters,
            key=lambda c: (c.count, c.severity_rank),
            reverse=True,
        )[:n]

    def errors(self, n: int = 20) -> list[TemplateCluster]:
        return sorted(
            [c for c in self.clusters if c.is_error],
            key=lambda c: (c.severity_rank, c.count),
            reverse=True,
        )[:n]


# ---------------------------------------------------------------------------
# Drain parse tree
# ---------------------------------------------------------------------------


class _DrainNode:
    """A node in the Drain parse tree."""

    __slots__ = ("children", "clusters")

    def __init__(self) -> None:
        self.children: dict[str, _DrainNode] = {}
        self.clusters: list[TemplateCluster] = []


class DrainTemplateMiner:
    """Online log template miner inspired by the Drain algorithm.

    Parameters
    ----------
    depth:
        Fixed depth of the parse tree. Higher values improve accuracy at the
        cost of more memory.
    sim_threshold:
        Minimum token-overlap similarity for a message to join an existing
        cluster. Between 0 and 1.
    max_children:
        Maximum number of children per node. Prevents unbounded branching
        when messages have very high token diversity.
    """

    def __init__(
        self,
        *,
        depth: int = 4,
        sim_threshold: float = 0.5,
        max_children: int = 100,
    ) -> None:
        if depth < 1:
            raise ValueError("depth must be >= 1")
        if not 0.0 <= sim_threshold <= 1.0:
            raise ValueError("sim_threshold must be between 0 and 1")
        if max_children < 1:
            raise ValueError("max_children must be >= 1")

        self.depth = depth
        self.sim_threshold = sim_threshold
        self.max_children = max_children

        self._root = _DrainNode()
        self._next_cluster_id = 1
        self._total_events = 0

    # -- public API --------------------------------------------------------

    def add_event(self, event: LogEvent) -> TemplateCluster:
        """Insert a single event into the miner and return its cluster."""
        self._total_events += 1

        masked = mask_variables(event.message)
        tokens = tokenize(masked)
        if not tokens:
            tokens = ["<EMPTY>"]

        cluster = self._match_or_create(tokens)
        self._update_cluster(cluster, event)
        return cluster

    def add_events(self, events: list[LogEvent]) -> None:
        for event in events:
            self.add_event(event)

    def clusters(self) -> list[TemplateCluster]:
        """Return every cluster discovered so far."""
        return list(self._iter_clusters())

    def build_report(self, events: list[LogEvent] | None = None) -> ClusterReport:
        """Run the miner over a fresh batch and return a report."""
        if events is not None:
            self.add_events(events)
        all_clusters = self.clusters()
        return ClusterReport(
            clusters=all_clusters,
            total_events=self._total_events,
            total_clusters=len(all_clusters),
        )

    # -- internals ---------------------------------------------------------

    def _iter_clusters(self):
        stack = [self._root]
        while stack:
            node = stack.pop()
            yield from node.clusters
            stack.extend(node.children.values())

    def _match_or_create(self, tokens: list[str]) -> TemplateCluster:
        node = self._traverse(tokens)
        best, similarity = self._best_match(node.clusters, tokens)
        if best is not None and similarity >= self.sim_threshold:
            self._merge_tokens(best, tokens)
            return best

        cluster = TemplateCluster(
            cluster_id=self._next_cluster_id,
            template_tokens=list(tokens),
        )
        self._next_cluster_id += 1
        node.clusters.append(cluster)
        return cluster

    def _traverse(self, tokens: list[str]) -> _DrainNode:
        node = self._root
        for index in range(min(self.depth - 1, len(tokens))):
            key = self._branch_key(tokens[index])
            child = node.children.get(key)
            if child is None:
                if len(node.children) >= self.max_children:
                    # Fall back to the first existing child so that the tree
                    # stays bounded even with extremely diverse input.
                    child = next(iter(node.children.values()))
                else:
                    child = _DrainNode()
                    node.children[key] = child
            node = child
        return node

    @staticmethod
    def _branch_key(token: str) -> str:
        if token.startswith("<") and token.endswith(">"):
            return token
        # Use the first character to balance the tree.
        return token[:1].lower() or "_"

    @staticmethod
    def _best_match(
        clusters: list[TemplateCluster], tokens: list[str]
    ) -> tuple[TemplateCluster | None, float]:
        best: TemplateCluster | None = None
        best_score = 0.0
        for cluster in clusters:
            score = _similarity(cluster.template_tokens, tokens)
            if score > best_score:
                best = cluster
                best_score = score
        return best, best_score

    @staticmethod
    def _merge_tokens(cluster: TemplateCluster, tokens: list[str]) -> None:
        """Replace positions that differ with a wildcard placeholder."""
        merged: list[str] = []
        for old, new in zip(cluster.template_tokens, tokens):
            if old == new:
                merged.append(old)
            elif old == "<*>" or new.startswith("<") or old.startswith("<"):
                merged.append("<*>")
            else:
                merged.append("<*>")
        # Keep any extra tokens from the incoming message
        if len(tokens) > len(cluster.template_tokens):
            merged.extend("<*>" for _ in tokens[len(cluster.template_tokens):])
        cluster.template_tokens = merged

    @staticmethod
    def _update_cluster(cluster: TemplateCluster, event: LogEvent) -> None:
        cluster.count += 1
        cluster.level_counts[event.level.upper()] += 1
        if event.service:
            cluster.service_counts[event.service] += 1
        if cluster.first_seen is None or event.timestamp < cluster.first_seen:
            cluster.first_seen = event.timestamp
        if cluster.last_seen is None or event.timestamp > cluster.last_seen:
            cluster.last_seen = event.timestamp
        if cluster.sample_event is None:
            cluster.sample_event = event


# ---------------------------------------------------------------------------
# Similarity metric
# ---------------------------------------------------------------------------


def _similarity(a: list[str], b: list[str]) -> float:
    """Token-overlap similarity between two token lists.

    Wildcard placeholders (``<*>`` and typed masks like ``<NUM>``) are
    treated as matching any token in the other sequence. Returns a value
    between 0 and 1.
    """
    if not a or not b:
        return 0.0
    max_len = max(len(a), len(b))
    matches = 0
    for i in range(min(len(a), len(b))):
        if _tokens_match(a[i], b[i]):
            matches += 1
    return matches / max_len


def _tokens_match(a: str, b: str) -> bool:
    if a == b:
        return True
    if a == "<*>" or b == "<*>":
        return True
    a_masked = a.startswith("<") and a.endswith(">")
    b_masked = b.startswith("<") and b.endswith(">")
    if a_masked and b_masked:
        return True
    if a_masked or b_masked:
        # A typed mask matches any non-mask token in that position.
        return True
    return False