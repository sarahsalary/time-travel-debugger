# RootRewind — Historical Incident Investigation

**RootRewind** provides historical log replay, evidence-based incident investigation, and root-cause hypothesis ranking for JSONL, logfmt, syslog, CSV, Windows EVTX and OpenTelemetry logs; Loki collection with checkpoints and distributed workers. Python 3.10+.

## Project, author, and publishing

**RootRewind** is the public product and Python distribution name. Install it with
`pip install rootrewind` **after the first successful PyPI publication**. Its primary CLI
command is `rootrewind`; the former `ttd` command and Python import `ttd` remain
available for backward compatibility.

- **Author / maintainer:** [sarahsalary](https://github.com/sarahsalary)
- **Contact:** [saraasaalari@yahoo.com](mailto:saraasaalari@yahoo.com) (public in package metadata and on the PyPI project page)
- **Source repository:** [github.com/sarahsalary/time-travel-debugger](https://github.com/sarahsalary/time-travel-debugger)
- **Distribution:** `rootrewind` (name not reserved until PyPI accepts the first release)
- **License:** MIT; see `LICENSE` for conditions and third-party attribution obligations.

The repository and default Windows installation folder retain `time-travel-debugger`
to avoid breaking existing scripts and links. The import module remains `ttd`.

The software analyzes **previously recorded logs and state-change events**. It is **not** a CPU-instruction-level reverse debugger and does not restore arbitrary in-memory state. Incident ranks are hypotheses, not proven causes.

## Install RootRewind from PyPI (after publication)

```bash
python -m pip install rootrewind
# optional: python -m pip install 'rootrewind[tui]'
rootrewind --help
```

These commands will only work *after* the package has been successfully uploaded to PyPI. See `docs/PUBLISHING_FA.md` for first publication.

## Default Windows installation

The recommended project location is:

```text
D:\Github_Projects\time-travel-debugger
```

Extract the archive, run `INSTALL_TO_DEFAULT_PATH.ps1` from PowerShell, then execute `scripts\install_windows.ps1` from the installed project. The installer creates `.venv` and installs this package locally. Nothing is downloaded except Python packages via pip. If the destination contains any files, the copy helper refuses to overwrite it; an empty destination folder is allowed.

Alternatively, place the extracted `time-travel-debugger` folder directly under `D:\Github_Projects` and run:

```powershell
cd D:\Github_Projects\time-travel-debugger
powershell -ExecutionPolicy Bypass -File scripts\install_windows.ps1
.\.venv\Scripts\rootrewind.exe trace examples\demo.jsonl --at 2024-10-26T14:30:01Z
.\.venv\Scripts\rootrewind.exe query examples\demo.jsonl "level=ERROR OR duration_ms > 500"
.\.venv\Scripts\rootrewind.exe report examples\demo.jsonl --output demo-report.html
```

## Linux / macOS

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pytest
rootrewind --help
rootrewind trace examples/demo.jsonl --at 2024-10-26T14:30:01Z
```

## New in v1.1 — incident investigation with timeline

This is **an integrated extension of the existing project**, not a separate application.
Use `rootrewind incidents FILE` to list failures and `rootrewind investigate FILE --error-index 2`
to inspect the precursors of a selected failure. Add `--output incident.html` for an
interactive, self-contained time scrubber or `--json-output incident.json` for evidence.

```powershell
cd D:\Github_Projects\time-travel-debugger
.\.venv\Scripts\rootrewind.exe incidents examples\incident-rca.jsonl
.\.venv\Scripts\rootrewind.exe investigate examples\incident-rca.jsonl --error-index 2 --minutes 10 --output incident-demo.html --json-output incident-demo.json
Start-Process .\incident-demo.html
```

**Interpretation:** this is a *hypothesis generator based on observed logs* — it does
not certify a root cause. Only explicitly linked spans support service arrows; events
after the failure are context only, not cause candidates. See
`docs/ROOT_CAUSE_ANALYSIS_FA.md` (Persian) for details.

A standalone example report is included as `examples/incident-report-demo.html`;
open it in a browser without running a server.

**For an existing Windows installation:** back up any local changes and compare versions before upgrading. `UPGRADE_EXISTING_PROJECT.ps1` is provided only for users upgrading from an older package, not for fresh installation.

## Main commands

Use either `rootrewind` or its backward-compatible alias `ttd` in the following examples.


| Command | Purpose |
|:--|:--|
| `ttd trace FILE --at ISO_TIME` | Reconstruct time-bounded event history |
| `ttd state FILE --at ISO_TIME` | Reconstruct application state |
| `ttd between FILE --start ... --end ...` | Time window |
| `ttd diff FILE --before ... --after ...` | State comparison |
| `ttd query FILE "level=ERROR AND service=api"` | TTDQL search |
| `ttd tree FILE`, `ttd deps FILE` | Trace trees / dependency edges |
| `ttd anomalies FILE`, `ttd errors FILE --cluster` | Statistical anomalies / log templates |
| `ttd incidents FILE`, `ttd investigate FILE --error-index N` | Select a failure; show precursor evidence and interactive historical report |
| `ttd replay FILE` | Textual TUI; requires `.[tui]` |
| `ttd watch FILE`, `ttd follow FILE` | Watch live logs |
| `ttd report FILE --output report.html` | Shareable HTML report |
| `ttd serve-otlp` | Local OTLP/HTTP receiver |
| `ttd sessions --help` | Persistent and named sessions |
| `ttd alerts --help`, `ttd rules --help`, `ttd correlate FILE` | Rule analysis, webhook configuration, history, multi-event matching |
| `ttd loki --help`, `ttd loki-pool --help` | Remote Loki collection / weighted instances |
| `ttd checkpoints --help` | Checkpoint inspection |
| `ttd distributed --help` | Parallel resume on shared storage |
| `ttd mcp FILE` | Model Context Protocol stdio server (optional) |

## Loki fetching

```bash
ttd loki '{app="api"}' --start 2024-10-26T00:00:00Z --end 2024-10-27T00:00:00Z --url http://localhost:3100 --mode single --output logs.jsonl
ttd loki '{app="api"}' --start 2024-10-26T00:00:00Z --end 2024-10-27T00:00:00Z --url http://localhost:3100 --mode adaptive --workers 4 --checkpoint-name incident --output incident.jsonl
ttd loki '{app="api"}' --start 2024-10-26T00:00:00Z --end 2024-10-27T00:00:00Z --url http://localhost:3100 --mode adaptive --workers 4 --checkpoint-name incident --output incident.jsonl --resume
```

Checkpointing is local to an exact query and time range. For multi-host fetching, initialize a shared job, then launch `distributed run` on each worker and `distributed merge` when finished. Distributed coordination requires a filesystem with cross-host locking and coherent writes, such as correctly configured SMB/NFS. Object storage mounts without POSIX/Windows locking guarantees are not supported.

```bash
ttd distributed init /shared/ttd my-incident --query '{app="api"}' --start 2024-10-26T00:00:00Z --end 2024-10-27T00:00:00Z --chunk-seconds 300
ttd distributed run /shared/ttd my-incident --url http://localhost:3100
ttd distributed status /shared/ttd my-incident
ttd distributed merge /shared/ttd my-incident
```

## Limitations and operational safety

- Loki requires a running server; offline tests use mocked HTTP responses and do not validate your cluster.
- EVTX requires `pip install -e '.[evtx]'`; TUI requires `.[tui]`; MCP requires `.[mcp]`.
- The exact replayed **application state** depends on state updates recorded in your logs. Without state snapshots or change events, reconstruction is observational rather than restoring in-memory program variables.
- Timestamps are normalized to UTC when no timezone was supplied. Do not infer real-world incident ordering from logs with unsynchronized clocks.
- `ttd loki` uses a local checkpoint and flushes output before marking a time range complete. Use a single writer to its output file.
- Logs may contain secrets. The project never uploads them automatically; enabling webhooks, MCP, or remote Loki connections can expose log content to destinations you configure.
- For distributed fetching, use `portalocker` on every machine and a filesystem offering compatible cross-host locks.

## Repository layout

`src/ttd/` has the core, query, anomaly, cluster, OTLP, alerting, TUI, Loki, checkpoint and distributed modules. `tests/` holds repeatable unit/integration tests. `examples/` provides input logs and configuration templates. `scripts/` has installers. `docs/` documents architecture and source reconciliation.

## Publishing and safety checklist

This repository is packaged for PyPI using standard `pyproject.toml` metadata and setuptools. CI checks tests on Linux and Windows. Manual GitHub Actions publishing (`.github/workflows/publish.yml`) uses OIDC Trusted Publishing; it does **not** contain an API token. Publication requires an account-controlled repository, a configured trusted publisher on PyPI/TestPyPI and available project name. See `docs/PUBLISHING_FA.md` and `docs/RELEASE_CHECKLIST_FA.md`.

Do not push `.env`, log files containing actual credentials, customer data, traces with identifiers, or local `.venv`. This repository ships illustrative examples only. The authorship field does not establish the legal provenance of any third-party contributions; review origin and license notices before public release.
