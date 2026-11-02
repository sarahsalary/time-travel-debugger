"""Evidence-first historical incident investigation.

This module ranks *observable precursors*, not established root causes. It
never uses events after the selected failure to propose a cause, and it
labels trace relationships separately from mere temporal proximity.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .indexer import TimeIndex
from .models import LogEvent

ERROR_LEVELS = frozenset({"ERROR", "CRITICAL", "FATAL"})
WARN_LEVELS = frozenset({"WARNING", "WARN"})
CHANGE_WORDS = frozenset({
    "deploy", "deployment", "release", "rollout", "config", "configuration",
    "restart", "failover", "migration", "upgrade", "feature_flag",
})


@dataclass(frozen=True)
class Evidence:
    event_id: str
    timestamp: str
    service: str
    kind: str
    strength: str  # trace-linked / service-linked / temporal-only
    score: int  # transparent ordering heuristic; NOT a probability
    explanation: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return vars(self).copy()


@dataclass(frozen=True)
class ServiceLink:
    caller: str
    callee: str
    trace_id: str
    parent_span_id: str
    child_span_id: str

    def to_dict(self) -> dict[str, str]:
        return vars(self).copy()


@dataclass
class IncidentReport:
    selected_error_index: int
    error_total: int
    incident_id: str
    anchor: dict[str, Any]
    range_start: str
    range_end: str
    lookback_minutes: float
    lookahead_minutes: float
    event_count: int
    by_service: dict[str, int]
    timeline: list[dict[str, Any]]
    evidence: list[Evidence]
    service_links: list[ServiceLink]
    summary: str
    limitations: list[str] = field(default_factory=list)
    omitted_from_timeline: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": "heuristic_evidence_first_v1",
            "warning": "Candidates are possible precursors, NOT verified root causes; scores are not probabilities.",
            "selected_error_index": self.selected_error_index,
            "error_total": self.error_total,
            "incident_id": self.incident_id,
            "anchor": self.anchor,
            "range_start": self.range_start,
            "range_end": self.range_end,
            "lookback_minutes": self.lookback_minutes,
            "lookahead_minutes": self.lookahead_minutes,
            "event_count": self.event_count,
            "by_service": self.by_service,
            "timeline": self.timeline,
            "evidence": [e.to_dict() for e in self.evidence],
            "service_links": [l.to_dict() for l in self.service_links],
            "summary": self.summary,
            "limitations": self.limitations,
            "omitted_from_timeline": self.omitted_from_timeline,
        }


def list_incidents(index: TimeIndex) -> list[dict[str, Any]]:
    """Enumerate error-level events in chronological order, including duplicates."""
    return [
        {"index": i, "timestamp": e.timestamp.isoformat(), "service": e.service or "(unknown)",
         "trace_id": e.trace_id, "message": e.message}
        for i, e in enumerate((e for e in index.events if e.level in ERROR_LEVELS), 1)
    ]


def _reference(event: LogEvent, original_index: int) -> dict[str, Any]:
    return {
        "event_id": f"E{original_index + 1:06d}",
        "timestamp": event.timestamp.isoformat(),
        "level": event.level,
        "service": event.service or "(unknown)",
        "trace_id": event.trace_id,
        "span_id": event.span_id,
        "parent_span_id": event.parent_span_id,
        "message": event.message,
        "extra": event.extra,
        "duration_ms": event.duration_ms,
    }


def _is_change(event: LogEvent) -> bool:
    hints = [str(event.extra.get(k, "")).lower() for k in ("event_type", "action", "change_type", "operation")]
    hints.append(event.message.lower())
    import re
    return any(any(w in re.findall(r"[a-z_]+", h) for w in CHANGE_WORDS) for h in hints)


def _links(events: list[tuple[int, LogEvent]]) -> list[ServiceLink]:
    """Only infer caller/callee from an explicit parent_span_id in the *same trace*."""
    grouped: dict[str, list[LogEvent]] = {}
    for _, e in events:
        if e.trace_id:
            grouped.setdefault(e.trace_id, []).append(e)
    links: set[ServiceLink] = set()
    for trace_id, group in grouped.items():
        parents: dict[str, LogEvent] = {}
        for e in group:
            if e.span_id and e.service:
                parents.setdefault(e.span_id, e)
        for e in group:
            if not e.parent_span_id or not e.service or not e.span_id:
                continue
            parent = parents.get(e.parent_span_id)
            if parent and parent.service and parent.service != e.service:
                links.add(ServiceLink(parent.service, e.service, trace_id, e.parent_span_id, e.span_id))
    return sorted(links, key=lambda link: (link.caller, link.callee, link.trace_id, link.child_span_id))


def analyze_incident(
    index: TimeIndex,
    *,
    error_index: int | None = None,
    at: datetime | None = None,
    trace_id: str | None = None,
    lookback_minutes: float = 15,
    lookahead_minutes: float = 2,
    max_timeline_events: int = 400,
    max_evidence: int = 20,
) -> IncidentReport:
    """Investigate a selected error, with strictly pre-failure candidate evidence.

    ``error_index`` is 1-based among *all* error-level events (same as
    ``ttd incidents``); ``at`` and ``trace_id`` can limit eligibility.
    Use at most one of ``error_index`` and ``at`` to avoid ambiguity.
    """
    if lookback_minutes <= 0 or lookahead_minutes < 0:
        raise ValueError("lookback_minutes must be > 0 and lookahead_minutes must be >= 0")
    if max_timeline_events < 1 or max_evidence < 1:
        raise ValueError("max_timeline_events and max_evidence must be positive")
    if error_index is not None and (error_index < 1 or at is not None):
        raise ValueError("--error-index must be positive and cannot be combined with --at")
    all_errors = [(idx, event) for idx, event in enumerate(index.events) if event.level in ERROR_LEVELS]
    if not all_errors:
        raise ValueError("No ERROR/CRITICAL/FATAL events found in this log")
    eligible = [(i, item) for i, item in enumerate(all_errors, 1)
                if (at is None or item[1].timestamp <= at)
                and (trace_id is None or item[1].trace_id == trace_id)]
    if error_index is not None:
        selected = next(((n, item) for n, item in eligible if n == error_index), None)
        if selected is None:
            raise ValueError(f"Error index {error_index} is unavailable under the selected filters; use 'ttd incidents FILE'")
    else:
        if not eligible:
            raise ValueError("No error matches the selected time/trace filters")
        selected = eligible[-1]
    selected_no, (anchor_id, anchor) = selected
    start = anchor.timestamp - timedelta(minutes=lookback_minutes)
    end = anchor.timestamp + timedelta(minutes=lookahead_minutes)
    indexed_window = [(idx, e) for idx, e in enumerate(index.events)
                      if start <= e.timestamp <= end]
    # Never consider post-incident events as candidate causes.
    previous = [(idx, e) for idx, e in indexed_window
                if (e.timestamp, idx) < (anchor.timestamp, anchor_id)]
    previous.sort(key=lambda item: (item[1].timestamp, item[0]))
    anchor_ref = _reference(anchor, anchor_id)
    evidence: list[Evidence] = []
    for idx, e in previous:
        same_trace = bool(anchor.trace_id and e.trace_id == anchor.trace_id)
        same_service = bool(anchor.service and e.service == anchor.service)
        changed = _is_change(e)
        if same_trace and e.level in ERROR_LEVELS:
            kind, strength, score = "earlier_trace_error", "trace-linked", 90
            reason = "Earlier error in the same trace; examine whether it contributed to the selected failure."
        elif same_trace and e.level in WARN_LEVELS:
            kind, strength, score = "earlier_trace_warning", "trace-linked", 78
            reason = "Earlier warning in the same trace; inspect the operation and dependencies."
        elif same_trace and changed:
            kind, strength, score = "trace_change", "trace-linked", 68
            reason = "Change/restart/deploy-related event in the same trace; correlation does not prove cause."
        elif same_service and e.level in ERROR_LEVELS:
            kind, strength, score = "earlier_service_error", "service-linked", 60
            reason = "Earlier error in the same service without an established request-level link."
        elif same_service and e.level in WARN_LEVELS:
            kind, strength, score = "earlier_service_warning", "service-linked", 53
            reason = "Earlier warning in the same service; relationship to the failure is unverified."
        elif changed:
            kind, strength, score = "nearby_change", "temporal-only", 32
            reason = "A nearby deployment/configuration-type event; timing alone cannot establish causality."
        elif e.level in ERROR_LEVELS:
            kind, strength, score = "nearby_error", "temporal-only", 25
            reason = "Another error occurred before this incident; no explicit linkage was recorded."
        else:
            continue
        # Time proximity is only a tie-breaker, not a causal inference.
        minutes_ago = (anchor.timestamp - e.timestamp).total_seconds() / 60
        score = min(99, score + (3 if minutes_ago <= 1 else 1 if minutes_ago <= 5 else 0))
        ref = _reference(e, idx)
        evidence.append(Evidence(ref["event_id"], ref["timestamp"], ref["service"],
                                 kind, strength, score, reason, e.message))
    evidence.sort(key=lambda e: (-e.score, e.timestamp, e.event_id))
    evidence = evidence[:max_evidence]
    links = _links(indexed_window)
    by_service: dict[str, int] = {}
    for _, event in indexed_window:
        key = event.service or "(unknown)"
        by_service[key] = by_service.get(key, 0) + 1

    # Prioritize evidence, selected anchor, then recent events when a window is big.
    must_keep = {anchor_id}
    ev_ids = {e.event_id for e in evidence}
    must_keep.update(idx for idx, e in indexed_window if f"E{idx + 1:06d}" in ev_ids)
    chosen = list(indexed_window)
    if len(chosen) > max_timeline_events:
        essential = [item for item in chosen if item[0] in must_keep]
        if len(essential) >= max_timeline_events:
            chosen = essential[:max_timeline_events - 1] + [(anchor_id, anchor)]
        else:
            other = [item for item in chosen if item[0] not in must_keep]
            other.sort(key=lambda item: (abs((anchor.timestamp - item[1].timestamp).total_seconds()), item[0]))
            chosen = essential + other[:max_timeline_events - len(essential)]
        chosen = sorted(set(idx for idx, _ in chosen))
        chosen = [(idx, index.events[idx]) for idx in chosen]

    timeline = []
    evidence_ids = {e.event_id for e in evidence}
    for idx, event in chosen:
        r = _reference(event, idx)
        r["phase"] = "before" if (event.timestamp, idx) < (anchor.timestamp, anchor_id) else "selected_error" if idx == anchor_id else "after"
        r["same_trace"] = bool(anchor.trace_id and event.trace_id == anchor.trace_id)
        r["is_evidence"] = r["event_id"] in evidence_ids
        timeline.append(r)

    if evidence:
        top = evidence[0]
        summary = (f"Highest-ranked observable precursor: {top.service} / {top.kind} "
                   f"({top.event_id}, {top.strength}). This is a hypothesis to investigate, not a verified root cause.")
    else:
        summary = "No qualifying precursor was found in the selected lookback window; the root cause is undetermined."
    limitations = [
        "Evidence is derived exclusively from supplied log events; missing logs and unsynchronized clocks may invalidate ordering.",
        "A shared trace ID or temporal proximity indicates association, not proof of causation.",
        "Scores only rank fixed heuristics; they are not calibrated probabilities or machine-learning predictions.",
        "Events recorded after the selected error are displayed for context but never ranked as precursors.",
        "Service arrows are shown only for explicit same-trace parent/child span IDs, not for arbitrary adjacent log entries.",
    ]
    if not anchor.trace_id:
        limitations.append("The selected error has no trace ID; request-level correlation is unavailable.")
    return IncidentReport(
        selected_error_index=selected_no, error_total=len(all_errors),
        incident_id=anchor_ref["event_id"], anchor=anchor_ref,
        range_start=start.isoformat(), range_end=end.isoformat(),
        lookback_minutes=lookback_minutes, lookahead_minutes=lookahead_minutes,
        event_count=len(indexed_window), by_service=by_service, timeline=timeline,
        evidence=evidence, service_links=links, summary=summary,
        limitations=limitations, omitted_from_timeline=len(indexed_window) - len(chosen),
    )
