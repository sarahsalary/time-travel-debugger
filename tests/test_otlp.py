"""OpenTelemetry payload validation."""
from ttd.otlp import decode_logs_payload
from ttd.otlp_receiver import LiveStore


def test_otlp_log_decode():
    payload={"resourceLogs":[{"resource":{"attributes":[{"key":"service.name","value":{"stringValue":"api"}}]},"scopeLogs":[{"logRecords":[{"timeUnixNano":"1729953000000000000","severityText":"ERROR","body":{"stringValue":"Connection failed"}}]}]}]}
    events=decode_logs_payload(payload)
    assert len(events)==1
    assert events[0].message=='Connection failed'
    assert events[0].service=='api'
    store=LiveStore()
    store.append(events)
    assert len(store.snapshot())==1
