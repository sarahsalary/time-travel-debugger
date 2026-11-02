"""Behavioral tests for core time-travel functionality."""
from datetime import datetime, timezone
from pathlib import Path

from typer.testing import CliRunner

from ttd.cli import app
from ttd.cluster import DrainTemplateMiner
from ttd.differ import diff_states
from ttd.formats import detect_format, open_log_file
from ttd.graph import build_trace_tree, detect_dependencies
from ttd.indexer import TimeIndex
from ttd.models import LogEvent
from ttd.query import Query
from ttd.report.generator import HTMLReporter
from ttd.state import reconstruct_state


def moment(value="2024-10-26T14:30:01Z"):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_jsonl_roundtrip_and_zero_duration():
    event = LogEvent.from_raw({"time":"2024-10-26T14:30:00Z", "duration_ms":0, "message":"ok", "extra":{"id":1}, "host":"edge"})
    assert event.duration_ms == 0
    assert event.extra == {"id":1,"host":"edge"}
    again = LogEvent.from_raw(event.model_dump(mode="json"))
    assert again.extra == event.extra


def test_temporal_queries_and_state(demo):
    idx = TimeIndex.from_file(demo)
    assert len(idx) == 6
    assert len(idx.at(moment())) == 4
    state = reconstruct_state(idx, moment())
    assert state.event_count == 4
    assert state.fields["http_status"] == 504
    delta = diff_states(idx, moment("2024-10-26T14:30:00Z"), moment())
    assert "api" in delta.added_services


def test_query_parsing(demo):
    records = TimeIndex.from_file(demo).events
    matching = Query('level=ERROR AND duration_ms > 500').filter(records)
    assert len(matching) == 1
    assert matching[0].service == "api"
    assert len(Query('NOT level=DEBUG').filter(records)) == 6


def test_graph_and_cluster(demo):
    events = TimeIndex.from_file(demo).events
    traces = build_trace_tree(events)
    assert len(traces["demo-1"].events) == 4
    assert ("gateway", "api") in detect_dependencies(events)
    miner = DrainTemplateMiner()
    report = miner.build_report([e for e in events if e.level == "ERROR"])
    assert report.total_clusters == 1


def test_format_detection_and_reading(root):
    for filename, fmt in [("sample.csv", "csv"), ("sample.logfmt", "logfmt"), ("sample.syslog", "syslog")]:
        source = root / "examples" / filename
        assert detect_format(source).name == fmt
        assert len(list(open_log_file(source).stream())) == 2


def test_html_report(demo, tmp_path):
    output = tmp_path / "report.html"
    HTMLReporter(TimeIndex.from_file(demo), log_file=str(demo)).write(output)
    data = output.read_text(encoding="utf-8")
    assert "<html" in data.lower() and "api" in data.lower()
    assert len(data) > 1000


def test_cli_trace_and_query(demo):
    runner = CliRunner()
    result = runner.invoke(app, ["trace", str(demo), "--at", "2024-10-26T14:30:01Z", "--format", "json"])
    assert result.exit_code == 0, result.output
    assert "Connection timeout" in result.output
    result = runner.invoke(app, ["query", str(demo), "level=ERROR", "--format", "jsonl"])
    assert result.exit_code == 0, result.output
    assert result.output.count('"timestamp"') == 1


def test_nested_fields_function_calls_like_and_in(demo):
    records=TimeIndex.from_file(demo).events
    assert len(Query("extra.http_status=504").filter(records))==1
    assert len(Query("message ILIKE '%TIMEOUT%'").filter(records))==1
    assert len(Query("level IN (INFO, WARNING)").filter(records))==5
    assert len(Query("contains(lower(message), 'timeout')").filter(records))==1
    assert len(Query("hour(timestamp)=14 AND len(message)>12").filter(records))==6
