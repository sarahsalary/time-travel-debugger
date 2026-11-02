"""MCP (Model Context Protocol) server exposing ttd to AI agents.

This module implements a small, dependency-free JSON-RPC server that speaks
the Model Context Protocol over stdio. AI assistants such as Claude Code,
Cursor, and Cline can connect to it and use the exposed tools to query,
analyze, and debug structured log files.

Tools exposed:
    search_logs       — Query events with a TTDQL expression.
    get_stats         — Summary statistics about the log file.
    find_errors       — Return the most recent error events.
    trace_request     — Return every event belonging to a trace ID.
    detect_anomalies  — Run the statistical anomaly detector.
    cluster_errors    — Group similar error messages into templates.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from .anomaly import AnomalyDetector
from .cluster import DrainTemplateMiner
from .indexer import TimeIndex
from .query import Query, QuerySyntaxError


MCP_VERSION = "2024-11-05"
SERVER_NAME = "ttd"
SERVER_VERSION = "0.1.0"


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS: list[dict[str, Any]] = [
    {
        "name": "search_logs",
        "description": (
            "Search log events using a TTDQL expression. "
            "Examples: 'level=ERROR', 'duration_ms > 500', "
            "'service IN (api, db) AND message ~ \"timeout\"'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "TTDQL expression. Empty string returns all events.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of events to return (default 50).",
                    "default": 50,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_stats",
        "description": "Return summary statistics about the log file: total events, time range, levels, and services.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "find_errors",
        "description": "Return the most recent error-level events (ERROR, CRITICAL, FATAL).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of errors to return (default 20).",
                    "default": 20,
                },
            },
        },
    },
    {
        "name": "trace_request",
        "description": "Return every event that shares a given trace_id, in chronological order.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "trace_id": {
                    "type": "string",
                    "description": "The trace identifier to look up.",
                },
            },
            "required": ["trace_id"],
        },
    },
    {
        "name": "detect_anomalies",
        "description": (
            "Run statistical anomaly detection on the log stream. "
            "Returns windows whose volume or error rate deviates "
            "significantly from the rolling baseline."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "window": {
                    "type": "integer",
                    "description": "Window size in seconds (default 60).",
                    "default": 60,
                },
                "sensitivity": {
                    "type": "number",
                    "description": "Minimum absolute z-score (default 2.5).",
                    "default": 2.5,
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum anomalies to return (default 10).",
                    "default": 10,
                },
            },
        },
    },
    {
        "name": "cluster_errors",
        "description": (
            "Group similar error messages into templates using the Drain "
            "algorithm. Returns clusters sorted by count and severity."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Maximum clusters to return (default 20).",
                    "default": 20,
                },
                "query": {
                    "type": "string",
                    "description": "Optional TTDQL expression to pre-filter events.",
                    "default": "",
                },
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


class TTDMCPServer:
    """A JSON-RPC server that exposes ttd functionality as MCP tools."""

    def __init__(self, logfile: Path) -> None:
        self.logfile = logfile
        self.index = TimeIndex.from_file(logfile)
        self._initialized = False

    # -- JSON-RPC dispatch -------------------------------------------------

    def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        """Handle a single JSON-RPC request and return the response."""
        method = request.get("method")
        request_id = request.get("id")
        params = request.get("params", {})

        # Notifications (no id) do not expect a response.
        if request_id is None:
            return None

        try:
            if method == "initialize":
                result = self._initialize(params)
            elif method == "initialized":
                self._initialized = True
                return None
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                result = self._call_tool(params)
            elif method == "ping":
                result = {}
            else:
                return self._error(request_id, -32601, f"Method not found: {method}")
        except Exception as exc:  # noqa: BLE001 — surface any error to the client
            return self._error(request_id, -32603, f"Internal error: {exc}")

        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }

    # -- MCP methods -------------------------------------------------------

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "protocolVersion": MCP_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }

    def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments", {}) or {}

        dispatch = {
            "search_logs": self._tool_search_logs,
            "get_stats": self._tool_get_stats,
            "find_errors": self._tool_find_errors,
            "trace_request": self._tool_trace_request,
            "detect_anomalies": self._tool_detect_anomalies,
            "cluster_errors": self._tool_cluster_errors,
        }

        handler = dispatch.get(name)
        if handler is None:
            return self._tool_error(f"Unknown tool: {name}")

        try:
            payload = handler(arguments)
        except Exception as exc:  # noqa: BLE001
            return self._tool_error(str(exc))

        return {
            "content": [
                {"type": "text", "text": json.dumps(payload, default=str, indent=2)}
            ]
        }

    @staticmethod
    def _tool_error(message: str) -> dict[str, Any]:
        return {
            "content": [{"type": "text", "text": message}],
            "isError": True,
        }

    # -- Tool implementations ----------------------------------------------

    def _tool_search_logs(self, args: dict[str, Any]) -> dict[str, Any]:
        expression = args.get("query", "")
        limit = int(args.get("limit", 50))

        try:
            events = Query(expression).filter(self.index.events) if expression else list(self.index.events)
        except QuerySyntaxError as exc:
            raise ValueError(f"Invalid TTDQL expression: {exc}") from exc

        matched = len(events)
        return {
            "total_matched": matched,
            "returned": min(matched, limit),
            "events": [self._serialize(e) for e in events[:limit]],
        }

    def _tool_get_stats(self, args: dict[str, Any]) -> dict[str, Any]:
        levels: dict[str, int] = {}
        services: dict[str, int] = {}
        for event in self.index.events:
            levels[event.level] = levels.get(event.level, 0) + 1
            if event.service:
                services[event.service] = services.get(event.service, 0) + 1

        return {
            "file": str(self.logfile),
            "total_events": len(self.index),
            "start": self.index.start.isoformat() if self.index.start else None,
            "end": self.index.end.isoformat() if self.index.end else None,
            "levels": levels,
            "services": services,
        }

    def _tool_find_errors(self, args: dict[str, Any]) -> dict[str, Any]:
        limit = int(args.get("limit", 20))
        errors = [
            e for e in self.index.events
            if e.level.upper() in {"ERROR", "CRITICAL", "FATAL"}
        ]
        return {
            "total_errors": len(errors),
            "returned": min(len(errors), limit),
            "errors": [self._serialize(e) for e in errors[-limit:]],
        }

    def _tool_trace_request(self, args: dict[str, Any]) -> dict[str, Any]:
        trace_id = args.get("trace_id")
        if not trace_id:
            raise ValueError("trace_id is required")
        events = [e for e in self.index.events if e.trace_id == trace_id]
        events.sort(key=lambda e: e.timestamp)
        return {
            "trace_id": trace_id,
            "event_count": len(events),
            "events": [self._serialize(e) for e in events],
        }

    def _tool_detect_anomalies(self, args: dict[str, Any]) -> dict[str, Any]:
        window = int(args.get("window", 60))
        sensitivity = float(args.get("sensitivity", 2.5))
        limit = int(args.get("limit", 10))

        detector = AnomalyDetector(
            window_seconds=window,
            sensitivity=sensitivity,
        )
        report = detector.analyze(self.index.events)

        return {
            "window_seconds": window,
            "sensitivity": sensitivity,
            "total_anomalies": len(report.anomalies),
            "returned": min(len(report.anomalies), limit),
            "anomalies": [
                {
                    "window_start": a.window.start.isoformat(),
                    "window_end": a.window.end.isoformat(),
                    "metric": a.metric.value,
                    "value": a.value,
                    "baseline_mean": a.baseline_mean,
                    "baseline_std": a.baseline_std,
                    "z_score": a.z_score,
                    "severity": a.severity.value,
                    "direction": a.direction,
                }
                for a in report.top(limit)
            ],
        }

    def _tool_cluster_errors(self, args: dict[str, Any]) -> dict[str, Any]:
        limit = int(args.get("limit", 20))
        query = args.get("query", "")

        events = list(self.index.events)
        if query:
            try:
                events = Query(query).filter(events)
            except QuerySyntaxError as exc:
                raise ValueError(f"Invalid TTDQL expression: {exc}") from exc

        error_events = [
            e for e in events
            if e.level.upper() in {"ERROR", "CRITICAL", "FATAL"}
        ]

        miner = DrainTemplateMiner()
        miner.add_events(error_events)
        report = miner.build_report()

        return {
            "total_errors": len(error_events),
            "total_clusters": report.total_clusters,
            "clusters": [
                {
                    "cluster_id": c.cluster_id,
                    "template": c.template,
                    "count": c.count,
                    "error_count": c.error_count,
                    "first_seen": c.first_seen.isoformat() if c.first_seen else None,
                    "last_seen": c.last_seen.isoformat() if c.last_seen else None,
                    "services": dict(c.service_counts),
                }
                for c in report.top(limit)
            ],
        }

    # -- Serialization -----------------------------------------------------

    @staticmethod
    def _serialize(event: Any) -> dict[str, Any]:
        return {
            "timestamp": event.timestamp.isoformat(),
            "level": event.level,
            "service": event.service,
            "message": event.message,
            "trace_id": event.trace_id,
            "span_id": event.span_id,
            "parent_span_id": event.parent_span_id,
            "duration_ms": event.duration_ms,
            "extra": event.extra,
        }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_mcp_server(logfile: Path) -> None:
    """Run the MCP server over stdio until the client disconnects."""
    server = TTDMCPServer(logfile)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue

        response = server.handle(request)
        if response is None:
            continue
        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()