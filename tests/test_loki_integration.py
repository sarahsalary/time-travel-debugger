"""Unit tests for pagination, dynamic work queues and HTTP resilience."""
from datetime import datetime, timedelta, timezone

import pytest

from ttd.loki import LokiClient, LokiError
from ttd.loki_parallel import parallel_stream_range
from ttd.loki_backpressure import BackpressureConfig, ConcurrencyController
from ttd.loki_circuit import CircuitBreaker, CircuitConfig, CircuitOpenError
from ttd.loki_retry import RetryHandler, RetryPolicy, RetryableError
from ttd.models import LogEvent


def _response(first, second):
    return {"data":{"resultType":"streams", "result":[{"stream":{"app":"api", "level":"error"},"values":[[str(ts), msg] for ts,msg in [(first,"one"),(second,"two")]]}]}}


def test_loki_parses_nanosecond_timestamps():
    c = LokiClient('http://test:3100')
    records = list(c._parse_response(_response(1729953000000000000, 1729953001000000000)))
    assert len(records) == 2
    assert records[0].extra['loki_timestamp_ns'] == '1729953000000000000'
    assert records[0].service == 'api'


def test_loki_range_pagination_without_network(monkeypatch):
    c = LokiClient('http://test:3100',page_size=2)
    origin = datetime(2024,10,26,tzinfo=timezone.utc)
    ns = int(origin.timestamp() * 1e9)
    payloads = [_response(ns,ns+1_000_000_000), {'data':{'resultType':'streams','result':[]}}]
    calls=[]
    def fake_get(path, params, **kwargs):
        calls.append(params)
        return payloads.pop(0)
    monkeypatch.setattr(c, '_get', fake_get)
    results = list(c.stream_range('{app="api"}',start=origin,end=origin+timedelta(minutes=1)))
    assert len(results) == 2
    assert int(calls[1]['start']) == ns + 1_000_000_001


def test_backpressure_reacts_to_rate_limit():
    ctl = ConcurrencyController(BackpressureConfig(initial_limit=4,max_limit=8))
    ctl.record_failure(rate_limited=True)
    assert ctl.current_limit == 2


def test_retry_transient_failures():
    attempts = [0]
    handler = RetryHandler(RetryPolicy(max_attempts=3, base_delay_seconds=0.001), sleep=lambda seconds: None)
    def call():
        attempts[0]+=1
        if attempts[0]<3:
            raise RetryableError('HTTP 503')
        return 'OK'
    assert handler.call(call) == 'OK'
    assert attempts[0] == 3


def test_circuit_breaker_opens():
    breaker = CircuitBreaker(CircuitConfig(failure_threshold=2, reset_timeout_seconds=60.0))
    breaker.before_request(); breaker.on_failure()
    breaker.before_request(); breaker.on_failure()
    with pytest.raises(CircuitOpenError):
        breaker.before_request()


def test_parallel_ranges_no_duplicates():
    origin=datetime(2024,10,26,tzinfo=timezone.utc)
    data=[LogEvent.from_raw({'timestamp':(origin+timedelta(seconds=i)).isoformat(),'message':str(i)}) for i in range(16)]
    class FakeLoki:
        def stream_range(self, logql, *, start, end):
            return [e for e in data if start <= e.timestamp <= end]
    result=parallel_stream_range(FakeLoki(),'{app="test"}',origin,origin+timedelta(seconds=16),workers=2,adaptive=True,target_events_per_chunk=2,min_chunk_seconds=.5)
    assert [e.message for e in result] == [str(i) for i in range(16)]
