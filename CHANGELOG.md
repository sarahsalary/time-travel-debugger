# Changelog

## 1.1.2 — RootRewind naming and PyPI metadata

- Rebrand the public distribution and documentation to **RootRewind** (`pip install rootrewind` after publication).
- Retain `ttd` import and CLI for backward compatibility; add the `rootrewind` CLI entry point.
- Publish `sarahsalary` as author and maintainer, with the explicitly requested public contact address.
- Keep the established GitHub repository and Windows installation path unchanged.
- No material change to RCA scoring; all root-cause claims remain evidence-ranked hypotheses.


## 1.1.1 — sarahsalary public release preparation

- Set author/maintainer and MIT notice to `sarahsalary`.
- Set separate PyPI distribution name `sarahsalary-time-travel-debugger` (subject to registration availability); keep import `ttd` and CLI `ttd` unchanged.
- Add packaging metadata, automated build checks, opt-in trusted-publisher release workflow, bilingual installation/publishing instructions and safer empty-folder Windows installer.
- No change to the RCA scoring algorithm; operational validation against live external services remains the user's responsibility.


## 1.1.0 — historical incident investigation

- New `ttd incidents` command lists numbered error events.
- New `ttd investigate` command accepts an error index, as-of time or Trace ID;
  configurable minutes before/after; evidence-ranked possible precursor events.
- Cross-service links are built only from same-trace explicit parent/child span IDs.
- Time-travel HTML report with event scrubber, evidence links, service map, limitations.
- Structured JSON evidence export, safe HTML escaping, deterministic event IDs.
- New complete incident example, standalone demo report and 12 focused automated tests.
- Backup-based upgrade script for existing Windows installation at
  `D:\Github_Projects\time-travel-debugger`.

**Scientific/operational caution:** this is evidence-based hypothesis ranking, not
proven causal discovery. Timing, shared traces and the rank score do not prove cause.

## 1.0.0 — original complete package

Log parsing, time index, state reconstruction, diff, TTDQL, dependency trees,
anomalies and clustering, interactive TUI, HTML reports, OTLP, Loki,
checkpoints, correlation, rules, alerts and distributed processing.
