"""Unified command-line interface for RootRewind.

All commands share the same LogEvent model and automatic format registry.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .anomaly import AnomalyDetector
from .checkpoint import CheckpointState, CheckpointStore, CheckpointTracker
from .cluster import DrainTemplateMiner
from .correlation.config import load_correlation_rules
from .correlation.engine import CorrelationEngine
from .differ import diff_states
from .formats import detect_format
from .graph import build_trace_tree, detect_dependencies
from .history import HistoryStore
from .indexer import TimeIndex
from .loki import LokiClient
from .models import LogEvent
from .query import Query, QuerySyntaxError
from .report.generator import HTMLReporter
from .rules.config import load_rules
from .rules.engine import RuleEngine
from .session import SessionManager, SessionState
from .session_bundle import export_session, import_session, describe_bundle
from .state import reconstruct_state

app = typer.Typer(help="Historical replay and investigation of structured log files.", no_args_is_help=True, add_completion=False)
sessions_app = typer.Typer(help="Manage automatic and named workspaces.")
alerts_app = typer.Typer(help="Inspect alert delivery history and configuration.")
rules_app = typer.Typer(help="Run configurable alert rules.")
checkpoints_app = typer.Typer(help="Manage resumable Loki checkpoints.")
distributed_app = typer.Typer(help="Shared-storage parallel Loki workers.")
app.add_typer(sessions_app, name="sessions")
app.add_typer(alerts_app, name="alerts")
app.add_typer(rules_app, name="rules")
app.add_typer(checkpoints_app, name="checkpoints")
app.add_typer(distributed_app, name="distributed")
console = Console(stderr=False)
err_console = Console(stderr=True)


def _dt(value: str) -> datetime:
    from dateutil import parser
    try:
        parsed = parser.parse(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (ValueError, OverflowError) as exc:
        raise typer.BadParameter(f"Invalid date/time: {value}") from exc


def _load(path: Path) -> TimeIndex:
    try:
        return TimeIndex.from_file(path)
    except (OSError, ValueError, RuntimeError) as exc:
        err_console.print(f"[red]Cannot read input:[/red] {exc}")
        raise typer.Exit(2) from exc


def _filtered(events: list[LogEvent], expression: str | None, level: str | None = None, service: str | None = None) -> list[LogEvent]:
    if expression:
        try:
            events = Query(expression).filter(events)
        except QuerySyntaxError as exc:
            err_console.print(f"[red]TTDQL error:[/red] {exc}")
            raise typer.Exit(2) from exc
    if level:
        from .filters import Filters
        events = Filters().min_level(level).apply(events)
    if service:
        events = [e for e in events if e.service == service]
    return events


def _emit(events: list[LogEvent], fmt: str = "table", limit: int = 100) -> None:
    if fmt == "json":
        print(json.dumps([e.model_dump(mode="json") for e in events[:limit]], ensure_ascii=False, indent=2))
    elif fmt == "jsonl":
        for event in events[:limit]:
            print(event.model_dump_json())
    elif fmt == "table":
        table = Table(title=f"Events ({len(events)})")
        for col in ("Timestamp", "Level", "Service", "Trace", "Message"):
            table.add_column(col)
        for event in events[:limit]:
            table.add_row(event.timestamp.isoformat(timespec="milliseconds"), event.level, event.service or "-", event.trace_id or "-", event.short_message)
        console.print(table)
    else:
        raise typer.BadParameter("--format must be table, json, or jsonl")
    if len(events) > limit:
        err_console.print(f"[dim]Showing {limit} of {len(events)} events. Increase --limit to see more.[/dim]")


@app.command()
def version() -> None:
    """Print the installed package version."""
    console.print(f"ttd {__version__}")


@app.command()
def formats(logfile: Optional[Path] = typer.Argument(None, help="Optional log to identify")) -> None:
    """Show format auto-detection or list available format readers."""
    if logfile:
        parser = detect_format(logfile)
        console.print(f"{parser.name}: {parser.description}")
    else:
        console.print("jsonl, otlp-json, logfmt, syslog, csv/tsv, evtx (optional), plain")


@app.command()
def trace(logfile: Path, at: str = typer.Option(..., "--at", help="ISO timestamp"), query: str = typer.Option("", "--query", "-q"), level: Optional[str] = None, service: Optional[str] = None, limit: int = 100, format: str = "table") -> None:
    """Replay all events until a selected moment."""
    idx = _load(logfile)
    events = _filtered(idx.at(_dt(at)), query, level, service)
    _emit(events, format, limit)


@app.command()
def between(logfile: Path, start: str = typer.Option(..., "--start"), end: str = typer.Option(..., "--end"), query: str = typer.Option("", "--query", "-q"), limit: int = 100, format: str = "table") -> None:
    """Inspect the events in an inclusive time interval."""
    _emit(_filtered(_load(logfile).between(_dt(start), _dt(end)), query), format, limit)


@app.command()
def query(logfile: Path, expression: str, limit: int = 100, format: str = "table") -> None:
    """Filter events using a TTDQL expression."""
    _emit(_filtered(_load(logfile).events, expression), format, limit)


@app.command()
def state(logfile: Path, at: str = typer.Option(..., "--at")) -> None:
    """Reconstruct application health at a point in time."""
    data = reconstruct_state(_load(logfile), _dt(at))
    from dataclasses import asdict
    print(json.dumps(asdict(data), default=str, ensure_ascii=False, indent=2))


@app.command()
def diff(logfile: Path, before: str = typer.Option(...), after: str = typer.Option(...)) -> None:
    """Compare application state at two moments."""
    from dataclasses import asdict
    data = diff_states(_load(logfile), _dt(before), _dt(after))
    print(json.dumps(asdict(data), default=str, ensure_ascii=False, indent=2))


@app.command()
def tree(logfile: Path, trace_id: str = typer.Option("", "--trace-id")) -> None:
    """Show span trees grouped by distributed trace ID."""
    trees = build_trace_tree(_load(logfile).events)
    for trace_key, item in trees.items():
        if trace_id and trace_key != trace_id:
            continue
        console.print(f"[bold]Trace {trace_key}[/bold] ({len(item.events)} events)")
        def visit(node, depth=0):
            console.print("  " * depth + f"- {node.event.service or '-'}: {node.event.message}")
            for child in node.children:
                visit(child, depth + 1)
        for root in item.roots:
            visit(root)


@app.command()
def deps(logfile: Path) -> None:
    """Infer service dependency edges from traces."""
    pairs = detect_dependencies(_load(logfile).events)
    for src, target in pairs:
        console.print(f"{src} -> {target}")
    if not pairs:
        console.print("No cross-service dependencies found.")


@app.command()
def anomalies(logfile: Path, window: int = 60, sensitivity: float = 2.5, format: str = "table") -> None:
    """Detect statistical deviations in counts, error rates, and latency."""
    report = AnomalyDetector(window_seconds=window, sensitivity=sensitivity).analyze(_load(logfile).events)
    if format == "json":
        from dataclasses import asdict
        print(json.dumps([asdict(a) for a in report.anomalies], default=str, indent=2))
        return
    table = Table(title=f"Anomalies ({len(report.anomalies)})")
    for name in ("Window", "Metric", "Value", "Z-score", "Severity"):
        table.add_column(name)
    for a in report.anomalies:
        table.add_row(a.window.start.isoformat(), str(a.metric), f"{a.value:.3f}", f"{a.z_score:.2f}", str(a.severity))
    console.print(table)


@app.command()
def errors(logfile: Path, cluster: bool = typer.Option(False, "--cluster"), limit: int = 30) -> None:
    """Show errors, optionally clustered by Drain-style templates."""
    events = [e for e in _load(logfile).events if e.level in {"ERROR", "CRITICAL", "FATAL"}]
    if not cluster:
        _emit(events, "table", limit)
        return
    miner = DrainTemplateMiner()
    report = miner.build_report(events)
    table = Table(title=f"Error templates ({report.total_clusters})")
    table.add_column("Count"); table.add_column("Template")
    for c in report.clusters[:limit]:
        table.add_row(str(c.count), " ".join(c.template_tokens))
    console.print(table)


@app.command()
def report(logfile: Path, output: Path = typer.Option(Path("ttd-report.html"), "--output")) -> None:
    """Write a self-contained HTML incident report."""
    result = HTMLReporter(_load(logfile), log_file=str(logfile)).write(output)
    console.print(f"Saved HTML report: {result}")


@app.command("incidents")
def incidents(logfile: Path, limit: int = 50, format: str = "table") -> None:
    """List numbered failures; pass an index to `ttd investigate` for detailed analysis."""
    from .rca import list_incidents
    matches = list_incidents(_load(logfile))
    if limit < 1:
        raise typer.BadParameter("--limit must be positive")
    if format == "json":
        print(json.dumps(matches[:limit], ensure_ascii=False, indent=2))
        return
    if format != "table":
        raise typer.BadParameter("--format must be table or json")
    table = Table(title=f"Incidents ({len(matches)} errors)")
    for c in ("Index", "Time", "Service", "Trace", "Message"):
        table.add_column(c)
    for entry in matches[:limit]:
        table.add_row(str(entry["index"]), entry["timestamp"], entry["service"],
                      entry["trace_id"] or "-", entry["message"][:100])
    console.print(table)
    if len(matches) > limit:
        err_console.print(f"Showing {limit} of {len(matches)} errors; increase --limit to see more.")


@app.command("investigate")
def investigate(
    logfile: Path,
    error_index: Optional[int] = typer.Option(None, "--error-index", help="1-based error number from `ttd incidents`. Defaults to last matching error."),
    at: Optional[str] = typer.Option(None, "--at", help="Select the latest error at or before the given timestamp."),
    trace_id: Optional[str] = typer.Option(None, "--trace-id", help="Restrict incident selection to one trace."),
    minutes: float = typer.Option(15.0, "--minutes", help="Minutes to inspect BEFORE the failure."),
    after_minutes: float = typer.Option(2.0, "--after-minutes", help="Minutes to display AFTER failure (never used as causal evidence)."),
    output: Optional[Path] = typer.Option(None, "--output", help="Write standalone interactive .html report."),
    json_output: Optional[Path] = typer.Option(None, "--json-output", help="Write structured .json report."),
    format: str = typer.Option("table", "--format", help="table or json output on stdout."),
) -> None:
    """Investigate a historical failure, rank potential precursors and explore a timeline."""
    from .rca import analyze_incident
    from .rca_html import write_incident_html
    if format not in {"table", "json"}:
        raise typer.BadParameter("--format must be table or json")
    if output is not None and output.suffix.lower() != ".html":
        raise typer.BadParameter("--output must end in .html")
    if json_output is not None and json_output.suffix.lower() != ".json":
        raise typer.BadParameter("--json-output must end in .json")
    try:
        report_obj = analyze_incident(
            _load(logfile), error_index=error_index, at=_dt(at) if at else None,
            trace_id=trace_id, lookback_minutes=minutes, lookahead_minutes=after_minutes,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    payload = report_obj.to_dict()
    if output is not None:
        write_incident_html(report_obj, output)
        err_console.print(f"Interactive incident report: {output.resolve()}")
    if json_output is not None:
        json_output.parent.mkdir(parents=True, exist_ok=True)
        json_output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        err_console.print(f"JSON evidence report: {json_output.resolve()}")
    if format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return
    console.print(f"[bold]Selected failure #{report_obj.selected_error_index}[/bold]: "
                  f"{report_obj.anchor['timestamp']}  {report_obj.anchor['service']}  {report_obj.anchor['message']}")
    console.print(f"[dim]Window: {report_obj.range_start} to {report_obj.range_end} | "
                  f"{report_obj.event_count} events, {len(report_obj.by_service)} services[/dim]")
    console.print(report_obj.summary)
    table = Table(title="Possible precursors — NOT verified root causes")
    for col in ("Event ID", "Time", "Service", "Strength", "Rank", "Evidence"):
        table.add_column(col)
    for e in report_obj.evidence[:15]:
        table.add_row(e.event_id, e.timestamp, e.service, e.strength, str(e.score), e.message[:100])
    console.print(table)
    console.print("[yellow]Heuristic scores indicate rank, not confidence or probability. "
                  "Same-trace correlation and time ordering do not prove causality.[/yellow]")


@app.command()
def replay(logfile: Path, query: str = typer.Option("", "--query", "-q"), at: Optional[str] = typer.Option(None, "--at"), session: Optional[str] = typer.Option(None, "--session"), no_restore: bool = False) -> None:
    """Launch the interactive Textual replay interface (optional extra)."""
    try:
        from .tui import run_replay
    except ImportError as exc:
        err_console.print("Install the TUI dependency: pip install -e '.[tui]'")
        raise typer.Exit(2) from exc
    if session:
        record = SessionManager().get_named(session)
        if record:
            logfile = Path(record.log_file)
        else:
            SessionManager().create_named(session, logfile)
    run_replay(logfile, initial_query=query, at=at, restore=not no_restore)


@app.command()
def follow(logfile: Path, query: str = typer.Option("", "--query", "-q"), poll: float = 0.25) -> None:
    """Tail a growing log file until interrupted."""
    from .watch import FileTailer
    tailer = FileTailer(logfile, poll_interval=poll)
    selector = Query(query) if query else None
    console.print("Following new events; press Ctrl+C to stop.", style="dim")
    try:
        tailer.start_from_end()
        while True:
            for event in tailer.read_new():
                if selector is None or selector.matches(event):
                    print(event.model_dump_json(), flush=True)
            time.sleep(poll)
    except KeyboardInterrupt:
        pass


@app.command()
def watch(
    logfile: Path,
    query: str = typer.Option("", "--query", "-q"),
    window: int = 60,
    sensitivity: float = 2.5,
    alerts_config: Optional[Path] = None,
    rules_config: Optional[Path] = None,
    history: bool = True,
) -> None:
    """Watch live logs, applying local rules, alert history and configured webhooks."""
    from .watch import WatchEngine, Alert, AlertKind
    from .alerts.config import load_alert_config
    from .alerts.dispatcher import AlertDispatcher
    from .render import format_event
    engine = WatchEngine(logfile, query=query, window_seconds=window, sensitivity=sensitivity)
    settings = load_alert_config(alerts_config)
    dispatcher = AlertDispatcher(settings.channels, min_severity=settings.min_severity)
    rules = RuleEngine(load_rules(rules_config)) if rules_config else None
    hist = HistoryStore() if history else None
    def notify(alert: Alert, rule_name: str = "") -> None:
        err_console.print(f"[{alert.severity}] {alert.kind.value}: {alert.message}")
        if hist:
            hist.append(alert, source=str(logfile), rule_name=rule_name)
        dispatcher.dispatch(alert)
    engine.subscribe(notify)
    dispatcher.start()
    engine.start(from_end=True)
    observed = 0
    try:
        while engine.is_running:
            snapshot = engine.snapshot()
            if len(snapshot) > observed:
                latest = snapshot[observed:]
                for event in latest:
                    console.print(format_event(event))
                if rules:
                    rules.add_events(latest)
                    for result in rules.evaluate_all(now=latest[-1].timestamp):
                        if result.fired:
                            notify(Alert(AlertKind.ANOMALY, latest[-1].timestamp, result.message, result.severity, {"rule": result.rule_name}), rule_name=result.rule_name)
                observed = len(snapshot)
            time.sleep(0.25)
    except KeyboardInterrupt:
        pass
    finally:
        engine.stop()
        dispatcher.stop()


@app.command("serve-otlp")
def serve_otlp(host: str = "127.0.0.1", port: int = 4318, output: Optional[Path] = None) -> None:
    """Receive OTLP/HTTP logs and spans for local debugging."""
    from .otlp_receiver import OTLPReceiver
    receiver = OTLPReceiver(host=host, port=port)
    receiver.start(daemon=True)
    console.print(f"OTLP/HTTP receiver listening on http://{host}:{port}; Ctrl+C to stop")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        if output:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text("\n".join(e.model_dump_json() for e in receiver.store.snapshot()) + "\n", encoding="utf-8")
        receiver.stop()


@app.command("mcp")
def mcp(logfile: Path) -> None:
    """Expose indexed logs via an MCP stdio server (optional dependency)."""
    try:
        from .mcp_server import run_mcp_server
        run_mcp_server(logfile)
    except ImportError as exc:
        err_console.print("Install MCP support: pip install -e '.[mcp]'")
        raise typer.Exit(2) from exc


@app.command("correlate")
def correlate(logfile: Path, config: Optional[Path] = None) -> None:
    """Evaluate multi-event correlation rules against a log file."""
    rules = load_correlation_rules(config)
    engine = CorrelationEngine(rules)
    events = _load(logfile).events
    engine.add_events(events)
    matches = engine.evaluate(now=(events[-1].timestamp if events else datetime.now(timezone.utc)))
    console.print(json.dumps([m.__dict__ for m in matches], default=str, indent=2))


@sessions_app.command("list")
def sessions_list() -> None:
    mgr = SessionManager()
    console.print(json.dumps([s.__dict__ for s in mgr.list_named()], indent=2))


@sessions_app.command("save")
def sessions_save(name: str, logfile: Path, query: str = "", description: str = "", overwrite: bool = False) -> None:
    mgr = SessionManager()
    mgr.save(SessionState(log_file=str(logfile.resolve()), query=query))
    entry = mgr.create_named(name, logfile, description=description, overwrite=overwrite)
    console.print(f"Saved named session {entry.name}")


@sessions_app.command("show")
def sessions_show(name: str) -> None:
    mgr = SessionManager()
    entry = mgr.get_named(name)
    if not entry:
        raise typer.BadParameter(f"Unknown session: {name}")
    state = mgr.load(Path(entry.log_file))
    console.print(json.dumps({"session": entry.__dict__, "state": state.__dict__}, default=str, indent=2))


@sessions_app.command("delete")
def sessions_delete(name: str) -> None:
    console.print("Deleted" if SessionManager().delete_named(name) else "Not found")


@sessions_app.command("export")
def sessions_export(name: str, output: Path, include_log: bool = False) -> None:
    result = export_session(output, session_name=name, include_log=include_log)
    console.print(f"Exported {result}")


@sessions_app.command("import")
def sessions_import(bundle: Path, overwrite: bool = False) -> None:
    state = import_session(bundle, overwrite=overwrite)
    console.print(f"Imported session for {state.log_file}")


@alerts_app.command("history")
def alerts_history(limit: int = 50) -> None:
    console.print(json.dumps([x.__dict__ for x in HistoryStore().read(limit=limit)], default=str, indent=2))


@alerts_app.command("stats")
def alerts_stats() -> None:
    console.print(json.dumps(HistoryStore().stats(), default=str, indent=2))


@alerts_app.command("validate")
def alerts_validate(config: Optional[Path] = None) -> None:
    from .alerts.config import load_alert_config
    parsed = load_alert_config(config)
    console.print(f"Alert channel configuration loaded: {len(parsed.channels)} channels")


@rules_app.command("validate")
def rules_validate(config: Optional[Path] = None) -> None:
    console.print(f"Loaded {len(load_rules(config))} rules")


@rules_app.command("evaluate")
def rules_evaluate(logfile: Path, config: Optional[Path] = None) -> None:
    events = _load(logfile).events
    engine = RuleEngine(load_rules(config))
    engine.add_events(events)
    results = engine.evaluate_all(now=(events[-1].timestamp if events else datetime.now(timezone.utc)))
    console.print(json.dumps([r.__dict__ for r in results], default=str, indent=2))


def _client(urls: list[str], node: list[str], pool_config: Optional[Path], page_size: int) -> object:
    from .loki_pool import LokiNodeConfig
    if pool_config or node or len(urls) > 1:
        from .loki_pool_client import LokiPoolClient
        try:
            import tomllib
        except ModuleNotFoundError:
            import tomli as tomllib
        configs = []
        if pool_config:
            payload = tomllib.loads(pool_config.read_text(encoding="utf-8"))
            configs.extend(LokiNodeConfig(**row) for row in payload.get("nodes", []))
        for i, raw in enumerate(node):
            parts = raw.split("=", 1)
            if len(parts) < 2:
                raise typer.BadParameter("Node format: name=http://host:3100[:weight]")
            name, address = parts
            parts2 = address.rsplit(":", 1)
            weight = int(parts2[1]) if len(parts2) > 1 and parts2[1].isdigit() else 1
            endpoint = parts2[0] if len(parts2) > 1 and parts2[1].isdigit() else address
            configs.append(LokiNodeConfig(name=name, url=endpoint, weight=weight))
        for i, url in enumerate(urls):
            configs.append(LokiNodeConfig(name=f"node-{i+1}", url=url))
        return LokiPoolClient(configs)
    return LokiClient(urls[0] if urls else os.environ.get("TTD_LOKI_URL", "http://localhost:3100"), page_size=page_size)


def _download_range(client: object, expression: str, start: datetime, end: datetime, mode: str, page_size: int, workers: int) -> list[LogEvent]:
    if mode == "single":
        return list(client.stream_range(expression, start=start, end=end))
    if mode == "adaptive":
        from .loki_adaptive import AdaptiveConfig
        return list(client.adaptive_stream_range(expression, start=start, end=end, config=AdaptiveConfig(max_workers=workers)))
    if mode in {"parallel", "dynamic"}:
        from .loki_parallel import parallel_stream_range
        return parallel_stream_range(client, expression, start, end, workers=workers, page_size=page_size, adaptive=(mode == "dynamic"))
    raise typer.BadParameter("Mode must be single, parallel, adaptive, or dynamic")


@app.command()
def loki(expression: str, start: str = typer.Option(..., "--start"), end: str = typer.Option(..., "--end"), url: list[str] = typer.Option([], "--url"), node: list[str] = typer.Option([], "--node"), loki_config: Optional[Path] = None, mode: str = "single", workers: int = 4, page_size: int = 1000, output: Path = Path("loki-events.jsonl"), resume: bool = False, checkpoint_name: Optional[str] = None) -> None:
    """Fetch Loki logs with paging, parallelism, resilience and resumable checkpoints."""
    from hashlib import sha256
    begin, finish = _dt(start), _dt(end)
    if begin >= finish:
        raise typer.BadParameter("--start must precede --end")
    client = _client(url, node, loki_config, page_size)
    identity = checkpoint_name or sha256(f"{expression}|{begin.isoformat()}|{finish.isoformat()}|{mode}".encode()).hexdigest()[:16]
    store = CheckpointStore()
    state = store.load(identity) if resume else None
    if resume and state and (state.query != expression or state.original_start != begin.isoformat() or state.original_end != finish.isoformat()):
        raise typer.BadParameter("Saved checkpoint belongs to another query or time range")
    if state is None:
        state = CheckpointState(job_id=identity, query=expression, original_start=begin.isoformat(), original_end=finish.isoformat(), mode=mode, output_path=str(output.resolve()))
        if not resume and output.exists():
            raise typer.BadParameter(f"Output already exists: {output}. Choose another output path or --resume.")
        store.save(state)
    elif state.output_path and str(output.resolve()) != state.output_path:
        raise typer.BadParameter(f"Resume requires the original --output path: {state.output_path}")
    tracker = CheckpointTracker(store, state)
    gaps = state.remaining_gaps()
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with output.open("a", encoding="utf-8") as handle:
            for lower, upper in gaps:
                err_console.print(f"[dim]Fetching {lower.isoformat()} -> {upper.isoformat()}[/dim]")
                records = _download_range(client, expression, lower, upper, mode, page_size, workers)
                for item in records:
                    if lower <= item.timestamp < upper:
                        handle.write(item.model_dump_json() + "\n")
                handle.flush()
                os.fsync(handle.fileno())
                tracker.record_chunk_complete(lower, upper, len(records))
        tracker.mark_complete()
    except KeyboardInterrupt:
        tracker.mark_aborted()
        err_console.print(f"Interrupted. Resume with --resume --checkpoint-name {identity}")
        raise typer.Exit(130)
    except Exception:
        tracker.mark_aborted()
        raise
    console.print(f"Wrote {output}; checkpoint {identity} complete")


@app.command("loki-pool")
def loki_pool(loki_config: Path = typer.Option(..., "--loki-config")) -> None:
    """Display configured weighted Loki node status."""
    client = _client([], [], loki_config, 1000)
    console.print(json.dumps(client.snapshot(), default=str, indent=2))


@checkpoints_app.command("list")
def checkpoints_list() -> None:
    console.print(json.dumps([p.to_dict() for p in CheckpointStore().list_all()], indent=2))


@checkpoints_app.command("show")
def checkpoints_show(job_id: str) -> None:
    record = CheckpointStore().load(job_id)
    if record is None:
        raise typer.BadParameter("Unknown checkpoint")
    console.print(json.dumps(record.to_dict(), indent=2))


@checkpoints_app.command("delete")
def checkpoints_delete(job_id: str) -> None:
    console.print("Deleted" if CheckpointStore().delete(job_id) else "Not found")


@distributed_app.command("init")
def distributed_init(root: Path, job_id: str, query: str = typer.Option(..., "--query"), start: str = typer.Option(...), end: str = typer.Option(...), chunk_seconds: float = 300.0) -> None:
    """Create a shared-storage job manifest and time-range leases."""
    from .distributed.coordinator import DistributedCoordinator
    c = DistributedCoordinator(root, job_id)
    manifest = c.initialize_job(query=query, start=_dt(start), end=_dt(end), mode="single", chunk_seconds=chunk_seconds)
    console.print(f"Initialized {manifest.job_id}: {manifest.total_chunks} chunks")


@distributed_app.command("run")
def distributed_run(root: Path, job_id: str, url: str = "http://localhost:3100", max_chunks: Optional[int] = None) -> None:
    """Process available chunks as one independently scheduled worker."""
    from .distributed.coordinator import DistributedCoordinator
    from .distributed.fetcher import DistributedFetcher
    c = DistributedCoordinator(root, job_id)
    manifest = c.join()
    if manifest is None:
        raise typer.BadParameter("Initialize the distributed job first")
    client = LokiClient(url=url)
    try:
        result = DistributedFetcher(c, client, manifest.query).run(max_chunks=max_chunks)
    except KeyboardInterrupt:
        raise typer.Exit(130)
    console.print(json.dumps(result, indent=2))


@distributed_app.command("status")
def distributed_status(root: Path, job_id: str) -> None:
    from .distributed.coordinator import DistributedCoordinator
    c = DistributedCoordinator(root, job_id)
    c.join()
    console.print(json.dumps(c.progress(), indent=2))


@distributed_app.command("merge")
def distributed_merge(root: Path, job_id: str) -> None:
    from .distributed.coordinator import DistributedCoordinator
    c = DistributedCoordinator(root, job_id)
    c.join()
    console.print(f"Merged to {c.merge_results()}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
