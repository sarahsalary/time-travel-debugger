"""Embedded Jinja2 template for self-contained HTML reports.

The template is stored as a Python string so that `ttd` remains a single
installable package without needing to ship template files alongside the
code. All CSS is inlined; no external CDN or font is required, so the
generated report works offline and can be emailed as a single attachment.
"""

from __future__ import annotations


REPORT_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ttd report — {{ log_file }}</title>
<style>
  :root {
    --bg: #0f1115;
    --panel: #161a21;
    --panel-2: #1d222b;
    --border: #2a313d;
    --text: #d7dde5;
    --muted: #7d8794;
    --accent: #5eb8ff;
    --ok: #4ec9a0;
    --warn: #f0c674;
    --err: #ff6b6b;
    --crit: #ff3b6b;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font: 14px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace;
  }
  header {
    padding: 24px 32px;
    border-bottom: 1px solid var(--border);
    background: var(--panel);
  }
  header h1 { margin: 0 0 6px; font-size: 20px; }
  header .meta { color: var(--muted); font-size: 12px; }
  main { padding: 24px 32px 64px; max-width: 1400px; margin: 0 auto; }
  section { margin-bottom: 32px; }
  section h2 {
    font-size: 15px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--muted);
    border-bottom: 1px solid var(--border);
    padding-bottom: 8px;
    margin: 0 0 16px;
  }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; }
  .card {
    background: var(--panel-2);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 14px 16px;
  }
  .card .label { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em; }
  .card .value { font-size: 22px; margin-top: 4px; }
  .card .value.ok { color: var(--ok); }
  .card .value.warn { color: var(--warn); }
  .card .value.err { color: var(--err); }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--border); }
  th { color: var(--muted); font-weight: 600; font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; }
  td.num { text-align: right; font-variant-numeric: tabular-nums; }
  tr:hover td { background: rgba(94, 184, 255, 0.06); }
  .badge { display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 11px; }
  .badge.DEBUG { background: #2a313d; color: var(--muted); }
  .badge.INFO { background: #1c3a52; color: var(--accent); }
  .badge.WARNING, .badge.WARN { background: #4a3a17; color: var(--warn); }
  .badge.ERROR { background: #4a1d1d; color: var(--err); }
  .badge.CRITICAL, .badge.FATAL { background: #5c1230; color: var(--crit); }
  .sparkline { display: block; width: 100%; height: 40px; }
  .bar-row { display: flex; align-items: center; gap: 10px; margin: 4px 0; }
  .bar-row .name { width: 90px; color: var(--muted); font-size: 12px; }
  .bar-row .bar { flex: 1; height: 14px; background: var(--panel-2); border-radius: 3px; overflow: hidden; }
  .bar-row .fill { height: 100%; background: var(--accent); }
  .bar-row .count { width: 60px; text-align: right; color: var(--muted); font-size: 12px; }
  .empty { color: var(--muted); font-style: italic; }
  footer { color: var(--muted); font-size: 11px; text-align: center; padding: 24px; border-top: 1px solid var(--border); }
  code { background: var(--panel-2); padding: 1px 5px; border-radius: 3px; font-size: 12px; }
</style>
</head>
<body>
<header>
  <h1>ttd report</h1>
  <div class="meta">
    {{ log_file }} &middot; generated {{ generated_at }} &middot; ttd {{ ttd_version }}
  </div>
</header>

<main>

<section>
  <h2>Overview</h2>
  <div class="cards">
    <div class="card">
      <div class="label">Events</div>
      <div class="value">{{ stats.total_events }}</div>
    </div>
    <div class="card">
      <div class="label">Services</div>
      <div class="value">{{ stats.services | length }}</div>
    </div>
    <div class="card">
      <div class="label">Errors</div>
      <div class="value err">{{ stats.error_count }}</div>
    </div>
    <div class="card">
      <div class="label">Warnings</div>
      <div class="value warn">{{ stats.warning_count }}</div>
    </div>
    <div class="card">
      <div class="label">Time span</div>
      <div class="value">{{ stats.time_span }}</div>
    </div>
  </div>
</section>

{% if stats.levels %}
<section>
  <h2>Level distribution</h2>
  {% for level, count in stats.levels %}
  <div class="bar-row">
    <div class="name"><span class="badge {{ level }}">{{ level }}</span></div>
    <div class="bar"><div class="fill" style="width:{{ (count / stats.max_level * 100) | round(2) }}%"></div></div>
    <div class="count">{{ count }}</div>
  </div>
  {% endfor %}
</section>
{% endif %}

{% if timeline_svg %}
<section>
  <h2>Event timeline</h2>
  {{ timeline_svg | safe }}
</section>
{% endif %}

{% if anomalies %}
<section>
  <h2>Anomalies ({{ anomalies | length }})</h2>
  <table>
    <thead>
      <tr>
        <th>Window start</th>
        <th>Metric</th>
        <th class="num">Value</th>
        <th class="num">Baseline</th>
        <th class="num">Z-score</th>
        <th>Severity</th>
      </tr>
    </thead>
    <tbody>
      {% for a in anomalies %}
      <tr>
        <td>{{ a.window_start }}</td>
        <td>{{ a.metric }}</td>
        <td class="num">{{ a.value }}</td>
        <td class="num">{{ a.baseline }}</td>
        <td class="num">{{ a.z_score }}</td>
        <td><span class="badge {{ a.severity_badge }}">{{ a.severity }}</span></td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</section>
{% endif %}

{% if clusters %}
<section>
  <h2>Error clusters ({{ clusters | length }})</h2>
  <table>
    <thead>
      <tr>
        <th>#</th>
        <th class="num">Count</th>
        <th class="num">Errors</th>
        <th>Services</th>
        <th>Template</th>
      </tr>
    </thead>
    <tbody>
      {% for c in clusters %}
      <tr>
        <td>{{ c.cluster_id }}</td>
        <td class="num">{{ c.count }}</td>
        <td class="num">{{ c.error_count }}</td>
        <td>{{ c.services }}</td>
        <td><code>{{ c.template }}</code></td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</section>
{% endif %}

{% if dependencies %}
<section>
  <h2>Service dependencies</h2>
  <table>
    <thead>
      <tr><th>Caller</th><th></th><th>Callee</th></tr>
    </thead>
    <tbody>
      {% for caller, callee in dependencies %}
      <tr><td>{{ caller }}</td><td>&rarr;</td><td>{{ callee }}</td></tr>
      {% endfor %}
    </tbody>
  </table>
</section>
{% endif %}

{% if recent_errors %}
<section>
  <h2>Recent errors</h2>
  <table>
    <thead>
      <tr>
        <th>Time</th>
        <th>Level</th>
        <th>Service</th>
        <th>Message</th>
      </tr>
    </thead>
    <tbody>
      {% for e in recent_errors %}
      <tr>
        <td>{{ e.timestamp }}</td>
        <td><span class="badge {{ e.level }}">{{ e.level }}</span></td>
        <td>{{ e.service }}</td>
        <td>{{ e.message }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</section>
{% endif %}

</main>

<footer>
  Generated by ttd — RootRewind
</footer>
</body>
</html>
"""