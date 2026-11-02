# Architecture and guarantees

This repository is a consolidated implementation of the incremental RootRewind design. Earlier conversation snippets represented successive patches and cannot be concatenated verbatim. The source in `src/ttd` is the canonical maintained version.

## Flow

```
JSONL / OTLP / logfmt / syslog / CSV / EVTX
                    |
               FormatParser
                    |
                LogEvent
                    |
      +-------------+-------------+
      |             |             |
  TimeIndex       TTDQL         Watch
      |             |             |
 State/Diff    Filters       Anomaly/Rules
 Trace Trees   Correlation   Webhooks/History
      |             |             |
      +---------- Reports / TUI --+
                    |
          Optional MCP server

Remote Loki query_range + paging
   -> retry/backoff + circuit breaker
   -> weighted multi-instance pool
   -> parallel/adaptive or dynamically split work
   -> checkpointed JSONL output
   -> optional shared-file lease coordination
```

## Components

- `models.py`, `parser.py`, `formats/`: consistent event normalization.
- `indexer.py`, `state.py`, `differ.py`, `graph.py`: temporal lookup and diagnostic views.
- `query.py`: safe lexer/recursive-descent TTDQL; nested fields and scalar functions.
- `anomaly.py`, `cluster.py`, `correlation/`, `rules/`: offline analytics.
- `watch.py`, `alerts/`, `history.py`: live events, rule evaluation and notification history.
- `report/`: self-contained HTML reports.
- `tui/`: interactive Textual browsing; optional dependency.
- `otlp.py`, `otlp_receiver.py`: OTLP/HTTP JSON ingestion; protobuf support depends on installed decoder libraries.
- `loki.py`, `loki_adaptive.py`, `loki_parallel.py`, `loki_backpressure.py`, `loki_retry.py`, `loki_circuit.py`: resilient remote fetching.
- `loki_pool.py`, `loki_pool_client.py`: weighted routing among replicated Loki nodes; not a substitute for merging independent regional datasets.
- `checkpoint.py`, `shutdown.py`, `distributed/`: resumable local and multi-worker jobs.

## Verification status

Offline pytest exercises the CLI, indexing, timestamp and field preservation, TTDQL, formatting, report generation, Loki pagination under a mocked service, retry/backpressure/circuit states, checkpoint resume, OTLP decoding and local shared-storage coordination. A live Loki cluster, Textual TUI, MCP and Windows/SMB multi-host deployment are external integrations and require testing on the deployment machine.

## Known constraints

1. In-memory `TimeIndex` loads log records into RAM; for very large files, use time-scoped chunks rather than interpreting it as a disk-backed database.
2. Python datetimes have microsecond precision while Loki uses nanosecond timestamps. Loki raw timestamps are retained in `extra.loki_timestamp_ns`; local reconstructed dates use microseconds.
3. Loki's API does not provide general offset pagination for multiple records with exactly the same nanosecond timestamp. A full page ending in a duplicate timestamp is treated as ambiguous, not guaranteed complete.
4. In a distributed run, a lease's TTL must exceed the normal chunk duration or workers should call `renew_lease`; crashing mid-write leaves the chunk unfinished and eligible for retry. Each chunk writes one atomic output file, reducing duplicate replay after restart.
5. Checkpoints guarantee completed time-range accounting, not exactly-once transactions across independently modified servers. Do not manually edit checkpoint JSON while a collector is running.
6. Notifications require explicit configuration. No webhook destination is enabled by default.
