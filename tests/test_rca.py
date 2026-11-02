"""Focused incident investigation tests, including anti-hallucination and HTML XSS."""
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ttd.cli import app
from ttd.indexer import TimeIndex
from ttd.models import LogEvent
from ttd.rca import analyze_incident, list_incidents
from ttd.rca_html import render_incident_html


def time(seconds: int) -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds)


def event(seconds: int, level: str = "INFO", service: str = "api", message: str = "log", **kw):
    return LogEvent(timestamp=time(seconds), level=level, service=service, message=message, **kw)


def fixture():
    return TimeIndex([
        event(0, "INFO", "api", "baseline"),
        event(10, "WARNING", "db", "low capacity", trace_id="t", span_id="db", parent_span_id="api"),
        event(15, "ERROR", "db", "pool exhausted", trace_id="t", span_id="db", parent_span_id="api"),
        event(20, "ERROR", "api", "timeout", trace_id="t", span_id="api"),
        event(30, "ERROR", "api", "second failure", trace_id="other"),
    ])


def test_list_and_select_latest():
    idx = fixture()
    errors = list_incidents(idx)
    assert [x["index"] for x in errors] == [1, 2, 3]
    report = analyze_incident(idx)
    assert report.selected_error_index == 3
    assert report.anchor["message"] == "second failure"


def test_precursor_traced_and_ranked():
    report = analyze_incident(fixture(), error_index=2, lookback_minutes=2)
    assert report.anchor["message"] == "timeout"
    assert report.evidence[0].message == "pool exhausted"
    assert report.evidence[0].strength == "trace-linked"
    assert any(x.message == "low capacity" for x in report.evidence)
    assert all(x.timestamp <= report.anchor["timestamp"] for x in report.evidence)
    assert "NOT verified" in report.to_dict()["warning"]


def test_future_events_excluded_from_precursors():
    report = analyze_incident(fixture(), error_index=1, lookahead_minutes=10)
    assert any(e["phase"] == "after" for e in report.timeline)
    assert not any(e.message == "timeout" for e in report.evidence)
    assert not any(e.message == "second failure" for e in report.evidence)


def test_asof_and_trace_filter():
    report = analyze_incident(fixture(), at=time(21), trace_id="t")
    assert report.selected_error_index == 2
    with pytest.raises(ValueError, match="cannot be combined"):
        analyze_incident(fixture(), at=time(21), error_index=2)
    with pytest.raises(ValueError, match="unavailable"):
        analyze_incident(fixture(), error_index=3, trace_id="t")


def test_explicit_span_graph_only():
    report = analyze_incident(fixture(), error_index=2)
    assert {(x.caller, x.callee) for x in report.service_links} == {("api", "db")}
    other = TimeIndex([event(0, service="a", trace_id="t"), event(1, service="b", trace_id="t"),
                       event(2, level="ERROR", service="a", trace_id="t")])
    assert not analyze_incident(other).service_links


def test_time_only_is_not_trace_causality():
    idx = TimeIndex([event(1, "INFO", "cfg", "Deployment finished", extra={"event_type":"deployment"}),
                     event(2, "ERROR", "api", "timeout")])
    report = analyze_incident(idx)
    assert report.evidence[0].strength == "temporal-only"
    assert "timing alone" in report.evidence[0].explanation.lower()


def test_no_evidence_no_invented_root_cause():
    report = analyze_incident(TimeIndex([event(2, "ERROR", "api", "timeout")]))
    assert report.evidence == []
    assert "undetermined" in report.summary
    assert len(report.timeline) == 1


def test_validation_and_missing_logs():
    with pytest.raises(ValueError, match="No ERROR"):
        analyze_incident(TimeIndex([event(1)]))
    with pytest.raises(ValueError, match="must be"):
        analyze_incident(fixture(), lookback_minutes=0)
    with pytest.raises(ValueError, match="positive"):
        analyze_incident(fixture(), error_index=-1)


def test_timestamp_tie_never_uses_subsequent_event():
    idx = TimeIndex([event(0,"ERROR","db","first"), event(0,"ERROR","api","second")])
    one = analyze_incident(idx,error_index=1)
    assert not any(e.message == "second" for e in one.evidence)
    assert one.anchor["event_id"] == "E000001"


def test_html_escapes_inline_script_and_visible_text():
    malicious = '</script><script>alert("x")</script>'
    idx = TimeIndex([event(1,"ERROR","api",malicious)])
    html = render_incident_html(analyze_incident(idx))
    assert malicious not in html
    assert "\\u003c/script\\u003e" in html
    assert "const REPORT" in html
    assert 'id="scrubber"' in html
    assert "textContent" in html
    assert 'src="http' not in html


def test_limit_keeps_selected_error():
    idx = TimeIndex([event(i, "WARNING", "db", "noise", trace_id="t") for i in range(30)]
                    + [event(31, "ERROR", "api", "critical", trace_id="t")])
    r = analyze_incident(idx, max_timeline_events=3)
    assert r.omitted_from_timeline > 0
    assert any(x["event_id"] == r.incident_id for x in r.timeline)


def test_cli_incidents_and_investigate(tmp_path: Path, root: Path):
    runner = CliRunner()
    log = root / 'examples' / 'incident-rca.jsonl'
    result = runner.invoke(app, ["incidents", str(log), "--format", "json"])
    assert result.exit_code == 0, result.output
    found = json.loads(result.output)
    assert len(found) == 3
    html = tmp_path / 'incident.html'
    report_json = tmp_path / 'incident.json'
    result = runner.invoke(app, ["investigate", str(log), "--error-index", "2", "--minutes", "10",
                                  "--output", str(html), "--json-output", str(report_json), "--format", "json"])
    assert result.exit_code == 0, result.output
    assert html.is_file() and 'id="scrubber"' in html.read_text(encoding='utf-8')
    report = json.loads(report_json.read_text(encoding='utf-8'))
    assert report['anchor']['service'] == 'api'
    assert report['evidence'][0]['service'] == 'database'
    assert any(l['caller']=='api' and l['callee']=='database' for l in report['service_links'])
