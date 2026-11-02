"""Build execution trees and detect service dependencies."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .models import LogEvent


@dataclass
class SpanNode:
    """A node in an execution tree (one span, possibly with children)."""

    event: LogEvent
    children: list["SpanNode"] = field(default_factory=list)

    @property
    def span_id(self) -> str:
        return self.event.span_id or f"anon-{id(self.event)}"

    @property
    def label(self) -> str:
        service = self.event.service or "-"
        return f"[{service}] {self.event.short_message}"


@dataclass
class TraceTree:
    """All spans belonging to a single trace."""

    trace_id: str
    roots: list[SpanNode] = field(default_factory=list)
    events: list[LogEvent] = field(default_factory=list)

    @property
    def total_spans(self) -> int:
        return len(self.events)


def build_trace_tree(events: list[LogEvent]) -> dict[str, TraceTree]:
    """Group events by ``trace_id`` and build parent/child relationships."""
    by_trace: dict[str, list[LogEvent]] = defaultdict(list)
    for event in events:
        if event.trace_id:
            by_trace[event.trace_id].append(event)

    forest: dict[str, TraceTree] = {}
    for trace_id, trace_events in by_trace.items():
        nodes: dict[str, SpanNode] = {}
        for event in trace_events:
            nodes[event.span_id or f"anon-{id(event)}"] = SpanNode(event=event)

        roots: list[SpanNode] = []
        for event in trace_events:
            node = nodes[event.span_id or f"anon-{id(event)}"]
            parent_id = event.parent_span_id
            if parent_id and parent_id in nodes:
                nodes[parent_id].children.append(node)
            else:
                roots.append(node)

        forest[trace_id] = TraceTree(
            trace_id=trace_id,
            roots=roots,
            events=sorted(trace_events, key=lambda e: e.timestamp),
        )
    return forest


def detect_dependencies(events: list[LogEvent]) -> list[tuple[str, str]]:
    """Return directed edges ``(caller, callee)`` between services."""
    edges: set[tuple[str, str]] = set()
    by_trace: dict[str, list[LogEvent]] = defaultdict(list)
    for event in events:
        if event.trace_id and event.service:
            by_trace[event.trace_id].append(event)

    for trace_events in by_trace.values():
        ordered = sorted(trace_events, key=lambda e: e.timestamp)
        for first, second in zip(ordered, ordered[1:]):
            if first.service and second.service and first.service != second.service:
                edges.add((first.service, second.service))

    return sorted(edges)


def critical_path(tree: TraceTree) -> list[LogEvent]:
    """Return the longest chain of spans in a trace by duration."""
    if not tree.roots:
        return []

    def longest(node: SpanNode) -> tuple[float, list[LogEvent]]:
        best_duration = 0.0
        best_chain: list[LogEvent] = []
        for child in node.children:
            child_duration, child_chain = longest(child)
            if child_duration > best_duration:
                best_duration = child_duration
                best_chain = child_chain
        own = node.event.duration_ms or 0.0
        return own + best_duration, [node.event, *best_chain]

    best: list[LogEvent] = []
    best_total = 0.0
    for root in tree.roots:
        total, chain = longest(root)
        if total > best_total:
            best_total = total
            best = chain
    return best