# Feature inventory

| Layer | Implementation | Example command |
| --- | --- | --- |
| JSONL and temporal replay | `models.py`, `indexer.py`, `state.py` | `ttd trace examples/demo.jsonl --at 2024-10-26T14:30:01Z` |
| TTDQL with nested fields/functions | `query.py` | `ttd query examples/demo.jsonl "contains(message, 'timeout')"` |
| Trace trees, dependency graph, state differences | `graph.py`, `differ.py` | `ttd deps examples/demo.jsonl` |
| Statistical anomaly detection | `anomaly.py` | `ttd anomalies examples/demo.jsonl` |
| Drain-like error clustering | `cluster.py` | `ttd errors examples/demo.jsonl --cluster` |
| TUI interactive replay | `tui/` | `ttd replay examples/demo.jsonl` |
| OTLP JSON receiver | `otlp.py`, `otlp_receiver.py` | `ttd serve-otlp` |
| Watch and live event stream | `watch.py` | `ttd watch examples/demo.jsonl` |
| Named and automatic sessions, bundle export | `session.py`, `session_bundle.py` | `ttd sessions save investigation examples/demo.jsonl` |
| HTML incident report | `report/` | `ttd report examples/demo.jsonl` |
| logfmt, syslog, CSV, EVTX (optional) | `formats/` | `ttd formats examples/sample.csv` |
| Alert channels and audit trail | `alerts/`, `history.py` | `ttd alerts history` |
| User-defined alert rules | `rules/` | `ttd rules validate --config examples/config/rules.toml` |
| Multi-event correlation | `correlation/` | `ttd correlate examples/demo.jsonl` |
| MCP server (optional) | `mcp_server.py` | `ttd mcp examples/demo.jsonl` |
| Loki incremental paging | `loki.py` | `ttd loki --help` |
| Parallel / adaptive / dynamic time splitting | `loki_parallel.py`, `loki_adaptive.py` | `ttd loki --mode dynamic --help` |
| Backpressure, retry, circuit breaker | `loki_backpressure.py`, `loki_retry.py`, `loki_circuit.py` | see Loki collector implementation |
| Checkpoint and graceful interruptions | `checkpoint.py`, `shutdown.py` | `ttd checkpoints list` |
| Weighted Loki multi-instance routing | `loki_pool.py`, `loki_pool_client.py` | `ttd loki-pool --help` |
| Distributed parallel resume | `distributed/` | `ttd distributed --help` |
