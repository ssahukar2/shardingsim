#!/usr/bin/env python3
"""HTML report for micro-benchmark TSVs (Chart.js). Pipeline: bench-micro.sh → micro_post.py → this file."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def find_latest_for_report(results_dir: Path) -> Path | None:
    """Prefer aggregated TSV (one row per config, medians); else raw; else legacy micro-*.tsv."""
    agg = sorted(
        results_dir.glob("micro-agg-*.tsv"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if agg:
        return agg[0]
    raw = sorted(
        results_dir.glob("micro-raw-*.tsv"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if raw:
        return raw[0]
    legacy = [
        p
        for p in results_dir.glob("micro-*.tsv")
        if "micro-agg-" not in p.name and "micro-raw-" not in p.name
    ]
    legacy.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return legacy[0] if legacy else None


def read_rows(tsv_path: Path) -> list[dict]:
    rows: list[dict] = []
    with tsv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="\t")
        fieldnames = reader.fieldnames or ()
        has_oneway = "bench_oneway_ms" in fieldnames
        for r in reader:
            row = dict(r)
            if not has_oneway:
                row.setdefault("bench_oneway_ms", "")
            rows.append(row)
    return rows


def num(s: str | None, default: float | None = None) -> float | None:
    if s is None or s == "":
        return default
    try:
        return float(s)
    except ValueError:
        return default


def _uniq_ints(rows: list[dict], key: str) -> list[int]:
    out: set[int] = set()
    for r in rows:
        v = r.get(key)
        if v is None or str(v).strip() == "" or str(v).upper() == "NA":
            continue
        try:
            out.add(int(str(v).strip(), 10))
        except ValueError:
            continue
    return sorted(out)


def coverage_banner_html(rows: list[dict]) -> str:
    """Explain what grid this file actually contains; warn when it looks like smoke/quick only."""
    if not rows:
        return ""
    n = len(rows)
    suites = sorted({(r.get("suite") or "").strip() or "?" for r in rows})
    suite_str = ", ".join(suites)
    is_agg = bool((rows[0].get("throughput_tx_s_median") or "").strip())
    counts = _uniq_ints(rows, "count")
    threads = _uniq_ints(rows, "threads")
    batches = _uniq_ints(rows, "batch")
    kind = "Aggregated (one row per configuration)" if is_agg else "Raw (one row per repetition)"

    parts = [
        f"<p><strong>{kind}</strong> · <strong>{n}</strong> rows · "
        f"<strong>suite</strong> = <code>{suite_str}</code></p>",
        "<ul style='margin:0.4rem 0 0 1rem; color:var(--muted); font-size:0.88rem'>",
        f"<li>Distinct <strong>TX counts</strong>: {len(counts)} — {counts if len(counts) <= 20 else str(counts[:20]) + '…'}</li>",
        f"<li>Distinct <strong>threads</strong>: {len(threads)} — {threads}</li>",
        f"<li>Distinct <strong>batches</strong>: {len(batches)} — {batches}</li>",
        "</ul>",
    ]

    n_ok_vals = [r.get("n_ok") for r in rows if (r.get("n_ok") or "").strip().isdigit()]
    if n_ok_vals and all(int(x) == 1 for x in n_ok_vals):
        parts.append(
            "<p style='margin:0.6rem 0 0; font-size:0.82rem; color:var(--muted)'>"
            "When <code>n_ok</code> is 1, median = Q25 = Q75 (only one measured rep per configuration). "
            "Increase <code>BENCH_MICRO_REPS</code> in <code>bench-micro.sh</code> for spread in quartiles.</p>"
        )

    is_quick = any(s.strip() == "quick" for s in suites)
    looks_tiny = len(counts) <= 4 and len(threads) <= 1 and len(batches) <= 1
    if is_quick or looks_tiny:
        parts.append(
            "<div class='cov-warn'>"
            "<strong>This report is a narrow smoke grid</strong> (a few counts / one thread / one batch). "
            "That is expected for <code>suite=quick</code> or <code>make bench-micro-quick</code>. "
            "<strong>For broad coverage</strong> run <code>make bench-micro</code> (standard: full Cartesian product "
            "of many counts × threads 1…min(12,nproc) × six batch sizes × inproc + 2 ZMQ modes) or "
            "<code>make bench-micro-full</code>. That takes much longer — use "
            "<code>BENCH_MICRO_REPS=1</code> while iterating."
            "</div>"
        )

    return (
        "<div class='cov-box'><h2 style='margin:0 0 0.5rem; font-size:1rem; color:var(--accent)'>"
        "Coverage in this file</h2>"
        + "".join(parts)
        + "</div>"
    )


def build_html(rows: list[dict], source_name: str, generated_at: str, coverage_html: str) -> str:
    data_json = json.dumps(rows, ensure_ascii=False)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Micro benchmark — {source_name}</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
  <style>
    :root {{
      --bg: #0f1419;
      --panel: #1a2332;
      --text: #e7ecf3;
      --muted: #8b9cb3;
      --accent: #5b9bd5;
      --ok: #6abf69;
      --fail: #e07a7a;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      font-family: ui-sans-serif, system-ui, Segoe UI, Roboto, sans-serif;
      background: var(--bg);
      color: var(--text);
      margin: 0;
      padding: 1.25rem 1.5rem 3rem;
      line-height: 1.45;
    }}
    h1 {{ font-size: 1.35rem; font-weight: 600; margin: 0 0 0.25rem; }}
    .meta {{ color: var(--muted); font-size: 0.85rem; margin-bottom: 1.25rem; }}
    .grid {{ display: grid; gap: 1.25rem; }}
    @media (min-width: 900px) {{
      .grid-2 {{ grid-template-columns: 1fr 1fr; }}
    }}
    .card {{
      background: var(--panel);
      border-radius: 10px;
      padding: 1rem 1.1rem;
      border: 1px solid #2a3544;
    }}
    .card h2 {{
      font-size: 0.95rem;
      font-weight: 600;
      margin: 0 0 0.75rem;
      color: var(--accent);
    }}
    .chart-wrap {{ position: relative; height: 280px; }}
    .controls {{
      display: flex; flex-wrap: wrap; gap: 0.75rem;
      align-items: center; margin-bottom: 0.75rem;
    }}
    .controls label {{ font-size: 0.8rem; color: var(--muted); }}
    select, input[type="search"] {{
      background: #0d1117;
      color: var(--text);
      border: 1px solid #3d4f66;
      border-radius: 6px;
      padding: 0.35rem 0.5rem;
      font-size: 0.85rem;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 0.78rem;
    }}
    th, td {{ padding: 0.35rem 0.5rem; text-align: left; border-bottom: 1px solid #2a3544; }}
    th {{ cursor: pointer; user-select: none; color: var(--muted); font-weight: 600; }}
    th:hover {{ color: var(--accent); }}
    tr:hover td {{ background: rgba(91, 155, 213, 0.06); }}
    .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
    .fail {{ color: var(--fail); }}
    .ok {{ color: var(--ok); }}
    .toolbar {{ margin: 0.5rem 0 1rem; display: flex; flex-wrap: wrap; gap: 0.5rem; }}
    button {{
      background: #2a3f5f;
      color: var(--text);
      border: 1px solid #4a6a8f;
      border-radius: 6px;
      padding: 0.4rem 0.75rem;
      font-size: 0.8rem;
      cursor: pointer;
    }}
    button:hover {{ background: #354d73; }}
    .cov-box {{
      background: #141c28;
      border: 1px solid #2f4a66;
      border-radius: 10px;
      padding: 1rem 1.15rem;
      margin-bottom: 1.25rem;
    }}
    .cov-warn {{
      margin-top: 0.85rem;
      padding: 0.65rem 0.85rem;
      background: rgba(224, 122, 60, 0.12);
      border: 1px solid rgba(224, 122, 60, 0.45);
      border-radius: 8px;
      font-size: 0.86rem;
      color: #e8c4a8;
      line-height: 1.5;
    }}
  </style>
</head>
<body>
  <h1>Micro benchmark report</h1>
  <p class="meta">Source: <code>{source_name}</code> · Generated: {generated_at} UTC<span id="aggNote"></span></p>
  {coverage_html}

  <div class="toolbar">
    <button type="button" id="btnCsv">Download filtered rows as CSV</button>
  </div>

  <details class="card" style="margin-bottom:1rem; padding:0.75rem 1rem">
    <summary style="cursor:pointer; font-weight:600; color:var(--accent)">What am I looking at? (column guide)</summary>
    <dl style="margin:0.75rem 0 0; font-size:0.82rem; color:var(--muted); display:grid; gap:0.35rem 1.5rem; grid-template-columns: auto 1fr;">
      <dt style="margin:0; color:var(--text)">TX count</dt><dd style="margin:0">Total transactions in that run.</dd>
      <dt style="margin:0; color:var(--text)">Threads</dt><dd style="margin:0">OpenMP threads in the generator.</dd>
      <dt style="margin:0; color:var(--text)">Batch</dt><dd style="margin:0">Transactions per batch.</dd>
      <dt style="margin:0; color:var(--text)">Mode</dt><dd style="margin:0"><code>inproc</code> = no ZMQ; <code>zmq</code> = REQ/REP to receiver.</dd>
      <dt style="margin:0; color:var(--text)">Throughput</dt><dd style="margin:0">TX/s from generator wall time (higher = faster end-to-end for that config).</dd>
      <dt style="margin:0; color:var(--text)">Wall (ms)</dt><dd style="margin:0">Elapsed time for the benchmarked parallel region.</dd>
      <dt style="margin:0; color:var(--text)"><code>*_median</code> / <code>n_ok</code></dt><dd style="margin:0">Aggregated files: median across successful reps; OK = successful runs in that group.</dd>
      <dt style="margin:0; color:var(--text)">One-way (ms)</dt><dd style="margin:0">ZMQ only: avg delay from send timestamp until after <code>zmq_recv</code> (not full round-trip).</dd>
    </dl>
  </details>

  <div class="grid grid-2">
    <div class="card">
      <h2>Throughput vs TX count</h2>
      <div class="controls">
        <label>Threads <select id="c1_threads"></select></label>
        <label>Batch <select id="c1_batch"></select></label>
        <label>Mode <select id="c1_mode"><option value="">all</option><option value="inproc">inproc</option><option value="zmq">zmq</option></select></label>
      </div>
      <div class="chart-wrap"><canvas id="chart1"></canvas></div>
    </div>
    <div class="card">
      <h2>Wall time vs TX count</h2>
      <div class="controls">
        <label>Threads <select id="c2_threads"></select></label>
        <label>Batch <select id="c2_batch"></select></label>
        <label>Mode <select id="c2_mode"><option value="">all</option><option value="inproc">inproc</option><option value="zmq">zmq</option></select></label>
      </div>
      <div class="chart-wrap"><canvas id="chart2"></canvas></div>
    </div>
    <div class="card">
      <h2>Throughput vs batch size (fixed count)</h2>
      <div class="controls">
        <label>TX count <select id="c3_count"></select></label>
        <label>Threads <select id="c3_threads"></select></label>
      </div>
      <div class="chart-wrap"><canvas id="chart3"></canvas></div>
    </div>
    <div class="card">
      <h2>One-way latency (receiver, ms) vs TX count</h2>
      <p class="meta" style="margin-top:-0.5rem">Only rows with bench_oneway_ms present; zmq + verify=1.</p>
      <div class="chart-wrap"><canvas id="chart4"></canvas></div>
    </div>
  </div>

  <div class="card" style="margin-top:1.25rem">
    <h2>All runs</h2>
    <div class="controls">
      <label>Filter <input type="search" id="tableFilter" placeholder="substring match on any column…"/></label>
    </div>
    <div style="overflow-x:auto; max-height:420px; overflow-y:auto">
      <table id="dataTable"><thead id="thead"></thead><tbody id="tbody"></tbody></table>
    </div>
  </div>

  <script>
  const RAW_ROWS = {data_json};

  function parseRow(r) {{
    const isAgg = r.throughput_tx_s_median != null && String(r.throughput_tx_s_median).trim() !== "";
    let exit_ok = false;
    if (isAgg) {{
      const nf = parseInt(r.n_fail, 10);
      const ok = parseInt(r.n_ok, 10);
      exit_ok = !isNaN(ok) && ok > 0 && !isNaN(nf) && nf === 0;
    }} else {{
      const exit = parseInt(r.exit_code, 10);
      exit_ok = !isNaN(exit) && exit === 0;
    }}
    const tp = parseFloat(r.throughput_tx_s_median ?? r.throughput_tx_s);
    const wall = parseFloat(r.wall_ms_median ?? r.wall_ms);
    const owRaw = r.bench_oneway_ms_median ?? r.bench_oneway_ms;
    const oneway = owRaw === "" || owRaw == null ? null : parseFloat(owRaw);
    return {{
      ...r,
      _isAgg: isAgg,
      count: parseInt(r.count, 10),
      threads: parseInt(r.threads, 10),
      batch: parseInt(r.batch, 10),
      recv_verify: parseInt(r.recv_verify, 10),
      exit_ok,
      throughput: isNaN(tp) ? NaN : tp,
      wall_ms: isNaN(wall) ? NaN : wall,
      build_ms: parseFloat(r.sum_build_ms_median ?? r.sum_build_ms),
      submit_ms: parseFloat(r.sum_submit_ack_ms_median ?? r.sum_submit_ack_ms),
      oneway: oneway != null && !isNaN(oneway) ? oneway : null,
    }};
  }}

  const ROWS = RAW_ROWS.map(parseRow);
  (function() {{
    const el = document.getElementById("aggNote");
    if (ROWS.length && ROWS[0]._isAgg && el)
      el.textContent = " · Charts use median throughput / wall / one-way (aggregated runs)";
  }})();

  function uniqSorted(arr) {{
    return [...new Set(arr)].sort((a,b) => a-b);
  }}

  function uniqStr(arr) {{
    return [...new Set(arr)].sort();
  }}

  function seriesKey(r) {{
    return r.mode + " vz=" + r.recv_verify;
  }}

  const palette = [
    "#5b9bd5", "#ed7d31", "#a5a5a5", "#ffc000", "#4472c4",
    "#70ad47", "#264478", "#9e480e", "#636363", "#997300"
  ];

  function fillSelect(sel, values, allLabel) {{
    sel.innerHTML = "";
    if (allLabel) {{
      const o = document.createElement("option");
      o.value = "";
      o.textContent = allLabel;
      sel.appendChild(o);
    }}
    for (const v of values) {{
      const o = document.createElement("option");
      o.value = String(v);
      o.textContent = String(v);
      sel.appendChild(o);
    }}
  }}

  function rowsMatching(threads, batch, mode) {{
    return ROWS.filter(r =>
      r.exit_ok &&
      (threads === "" || r.threads === threads) &&
      (batch === "" || r.batch === batch) &&
      (mode === "" || r.mode === mode)
    );
  }}

  function buildLineDatasets(sub) {{
    const byKey = new Map();
    for (const r of sub) {{
      const k = seriesKey(r);
      if (!byKey.has(k)) byKey.set(k, []);
      byKey.get(k).push(r);
    }}
    const keys = [...byKey.keys()].sort();
    return keys.map((k, i) => {{
      const pts = byKey.get(k).sort((a,b) => a.count - b.count);
      return {{
        label: k,
        data: pts.map(p => ({{ x: p.count, y: p.throughput }})),
        borderColor: palette[i % palette.length],
        backgroundColor: "transparent",
        tension: 0.15,
        pointRadius: 3,
      }};
    }});
  }}

  function buildWallDatasets(sub) {{
    const byKey = new Map();
    for (const r of sub) {{
      const k = seriesKey(r);
      if (!byKey.has(k)) byKey.set(k, []);
      byKey.get(k).push(r);
    }}
    const keys = [...byKey.keys()].sort();
    return keys.map((k, i) => {{
      const pts = byKey.get(k).sort((a,b) => a.count - b.count);
      return {{
        label: k,
        data: pts.map(p => ({{ x: p.count, y: p.wall_ms }})),
        borderColor: palette[i % palette.length],
        backgroundColor: "transparent",
        tension: 0.15,
        pointRadius: 3,
      }};
    }});
  }}

  function chartByBatch(countVal, threadsVal) {{
    const sub = ROWS.filter(r =>
      r.exit_ok && r.count === countVal &&
      (threadsVal === "" || r.threads === threadsVal)
    );
    const byKey = new Map();
    for (const r of sub) {{
      const k = seriesKey(r);
      if (!byKey.has(k)) byKey.set(k, []);
      byKey.get(k).push(r);
    }}
    const keys = [...byKey.keys()].sort();
    return keys.map((k, i) => {{
      const pts = byKey.get(k).sort((a,b) => a.batch - b.batch);
      return {{
        label: k,
        data: pts.map(p => ({{ x: p.batch, y: p.throughput }})),
        borderColor: palette[i % palette.length],
        backgroundColor: "transparent",
        tension: 0.1,
        pointRadius: 3,
      }};
    }});
  }}

  function onewayDatasets() {{
    const sub = ROWS.filter(r =>
      r.exit_ok && r.oneway != null && !isNaN(r.oneway) && r.mode === "zmq" && r.recv_verify === 1
    );
    if (sub.length === 0) return [];
    const byThreads = new Map();
    for (const r of sub) {{
      if (!byThreads.has(r.threads)) byThreads.set(r.threads, []);
      byThreads.get(r.threads).push(r);
    }}
    const keys = [...byThreads.keys()].sort((a,b) => a-b);
    return keys.map((t, i) => {{
      const pts = byThreads.get(t).sort((a,b) => a.count - b.count);
      return {{
        label: "threads=" + t,
        data: pts.map(p => ({{ x: p.count, y: p.oneway }})),
        borderColor: palette[i % palette.length],
        backgroundColor: "transparent",
        tension: 0.15,
        pointRadius: 3,
      }};
    }});
  }}

  const chartOpts = {{
    responsive: true,
    maintainAspectRatio: false,
    plugins: {{
      legend: {{ labels: {{ color: "#c5d0de" }} }},
    }},
    scales: {{
      x: {{
        type: "linear",
        ticks: {{ color: "#8b9cb3" }},
        grid: {{ color: "rgba(139,156,179,0.12)" }},
      }},
      y: {{
        ticks: {{ color: "#8b9cb3" }},
        grid: {{ color: "rgba(139,156,179,0.12)" }},
      }},
    }},
  }};

  const t1 = document.getElementById("c1_threads");
  const b1 = document.getElementById("c1_batch");
  const m1 = document.getElementById("c1_mode");
  const t2 = document.getElementById("c2_threads");
  const b2 = document.getElementById("c2_batch");
  const m2 = document.getElementById("c2_mode");
  const c3c = document.getElementById("c3_count");
  const c3t = document.getElementById("c3_threads");

  const threadsList = uniqSorted(ROWS.map(r => r.threads));
  const batchList = uniqSorted(ROWS.map(r => r.batch));
  const countList = uniqSorted(ROWS.map(r => r.count));

  fillSelect(t1, threadsList, false);
  fillSelect(b1, batchList, false);
  fillSelect(t2, threadsList, false);
  fillSelect(b2, batchList, false);
  fillSelect(c3c, countList, false);
  fillSelect(c3t, threadsList, true);

  if (threadsList.length) {{
    t1.value = String(threadsList[0]);
    t2.value = String(threadsList[0]);
  }}
  if (batchList.length) {{
    b1.value = String(batchList.includes(32) ? 32 : batchList[0]);
    b2.value = String(batchList.includes(32) ? 32 : batchList[0]);
  }}
  if (countList.length) {{
    const mid = countList[Math.floor(countList.length / 2)];
    c3c.value = String(mid);
  }}

  let ch1, ch2, ch3, ch4;

  function refreshCharts() {{
    const th1 = parseInt(t1.value, 10);
    const ba1 = parseInt(b1.value, 10);
    const mo1 = m1.value;
    const ds1 = buildLineDatasets(rowsMatching(th1, ba1, mo1));
    if (ch1) ch1.destroy();
    ch1 = new Chart(document.getElementById("chart1"), {{
      type: "line",
      data: {{ datasets: ds1 }},
      options: {{ ...chartOpts, plugins: {{ ...chartOpts.plugins, title: {{ display: true, text: "tx/s", color: "#8b9cb3" }} }} }}
    }});

    const th2 = parseInt(t2.value, 10);
    const ba2 = parseInt(b2.value, 10);
    const mo2 = m2.value;
    const ds2 = buildWallDatasets(rowsMatching(th2, ba2, mo2));
    if (ch2) ch2.destroy();
    ch2 = new Chart(document.getElementById("chart2"), {{
      type: "line",
      data: {{ datasets: ds2 }},
      options: {{ ...chartOpts, plugins: {{ ...chartOpts.plugins, title: {{ display: true, text: "ms", color: "#8b9cb3" }} }} }}
    }});

    const cv = parseInt(c3c.value, 10);
    const tv = c3t.value === "" ? "" : parseInt(c3t.value, 10);
    const ds3 = chartByBatch(cv, tv);
    if (ch3) ch3.destroy();
    ch3 = new Chart(document.getElementById("chart3"), {{
      type: "line",
      data: {{ datasets: ds3 }},
      options: {{ ...chartOpts, plugins: {{ ...chartOpts.plugins, title: {{ display: true, text: "tx/s vs batch", color: "#8b9cb3" }} }} }}
    }});

    const ds4 = onewayDatasets();
    if (ch4) ch4.destroy();
    ch4 = new Chart(document.getElementById("chart4"), {{
      type: "line",
      data: {{ datasets: ds4 }},
      options: {{ ...chartOpts, plugins: {{ ...chartOpts.plugins, title: {{ display: true, text: "ms (one-way)", color: "#8b9cb3" }} }} }}
    }});
  }}

  [t1,b1,m1,t2,b2,m2,c3c,c3t].forEach(el => el.addEventListener("change", refreshCharts));
  refreshCharts();

  // --- table --- (column order: useful keys first, then rest alphabetically)
  const preferred = [
    "suite","rep","count","threads","batch","mode","recv_sleep_ms","recv_verify",
    "n_ok","n_fail","exit_code",
    "throughput_tx_s_median","throughput_tx_s","throughput_tx_s_q25","throughput_tx_s_q75",
    "wall_ms_median","wall_ms","wall_ms_q25","wall_ms_q75",
    "sum_build_ms_median","sum_build_ms","sum_submit_ack_ms_median","sum_submit_ack_ms",
    "bench_oneway_ms_median","bench_oneway_ms","bench_oneway_ms_q25","bench_oneway_ms_q75",
  ];
  const allKeys = RAW_ROWS.length ? Object.keys(RAW_ROWS[0]) : [];
  const cols = [
    ...preferred.filter(k => allKeys.includes(k)),
    ...allKeys.filter(k => !preferred.includes(k) && k !== "_isAgg").sort(),
  ];
  const thead = document.getElementById("thead");
  const trh = document.createElement("tr");
  let sortKey = "count";
  let sortDir = 1;
  for (const c of cols) {{
    const th = document.createElement("th");
    th.textContent = c + (sortKey === c ? (sortDir > 0 ? " ▲" : " ▼") : "");
    th.dataset.key = c;
    th.addEventListener("click", () => {{
      if (sortKey === c) sortDir *= -1;
      else {{ sortKey = c; sortDir = 1; }}
      renderTable();
    }});
    trh.appendChild(th);
  }}
  thead.appendChild(trh);

  const tbody = document.getElementById("tbody");
  const filterEl = document.getElementById("tableFilter");

  function renderTable() {{
    const q = (filterEl.value || "").toLowerCase();
    let list = ROWS.slice();
    if (q) {{
      list = list.filter(r => {{
        return cols.some(c => String(r[c] ?? "").toLowerCase().includes(q));
      }});
    }}
    list.sort((a,b) => {{
      const av = a[sortKey];
      const bv = b[sortKey];
      const an = parseFloat(av);
      const bn = parseFloat(bv);
      let cmp;
      if (!isNaN(an) && !isNaN(bn) && String(av).trim() !== "" && String(bv).trim() !== "")
        cmp = an - bn;
      else
        cmp = String(av).localeCompare(String(bv));
      return sortDir * cmp;
    }});
    tbody.innerHTML = "";
    for (const r of list) {{
      const tr = document.createElement("tr");
      const ok = r.exit_ok;
      for (const c of cols) {{
        const td = document.createElement("td");
        td.textContent = r[c] ?? "";
        if (/^(throughput_|wall_ms|sum_|bench_oneway|n_ok|n_fail)/.test(c) || ["throughput_tx_s","wall_ms","sum_build_ms","sum_submit_ack_ms","bench_oneway_ms","exit_code"].includes(c))
          td.className = "num";
        if (c === "exit_code" || c === "n_fail")
          td.className = "num " + (ok ? "ok" : "fail");
        tr.appendChild(td);
      }}
      tbody.appendChild(tr);
    }}
    // refresh header arrows
    [...thead.querySelectorAll("th")].forEach(th => {{
      const c = th.dataset.key;
      th.textContent = c + (sortKey === c ? (sortDir > 0 ? " ▲" : " ▼") : "");
    }});
  }}

  filterEl.addEventListener("input", () => renderTable());
  renderTable();

  document.getElementById("btnCsv").addEventListener("click", () => {{
    const q = (filterEl.value || "").toLowerCase();
    let list = ROWS.slice();
    if (q) {{
      list = list.filter(r => cols.some(c => String(r[c] ?? "").toLowerCase().includes(q)));
    }}
    const esc = (v) => {{
      const s = String(v ?? "");
      if (/[",\\n]/.test(s)) return '"' + s.replace(/"/g, '""') + '"';
      return s;
    }};
    const lines = [cols.join(",")];
    for (const r of list) {{
      lines.push(cols.map(c => esc(r[c])).join(","));
    }}
    const blob = new Blob([lines.join("\\n")], {{ type: "text/csv;charset=utf-8" }});
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "micro_filtered.csv";
    a.click();
    URL.revokeObjectURL(a.href);
  }});
  </script>
</body>
</html>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate HTML report from micro benchmark TSV.")
    ap.add_argument(
        "tsv",
        nargs="?",
        type=Path,
        help="Path to micro-*.tsv (default: newest results/micro-*.tsv)",
    )
    ap.add_argument(
        "-o", "--out",
        type=Path,
        help="Output HTML path (default: same stem as TSV with .html next to it)",
    )
    args = ap.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    results_dir = repo_root / "results"

    tsv_path = args.tsv
    if tsv_path is None:
        tsv_path = find_latest_for_report(results_dir)
        if tsv_path is None:
            print("No results/micro-raw-*.tsv or micro-agg-*.tsv found. Run make bench-micro first.", file=sys.stderr)
            return 1
    else:
        tsv_path = tsv_path.resolve()
        if not tsv_path.is_file():
            print(f"Not a file: {tsv_path}", file=sys.stderr)
            return 1

    rows = read_rows(tsv_path)
    if not rows:
        print(f"Empty TSV: {tsv_path}", file=sys.stderr)
        return 1

    out_path = args.out
    if out_path is None:
        out_path = tsv_path.with_suffix(".html")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    cov = coverage_banner_html(rows)
    html = build_html(rows, tsv_path.name, now, cov)
    out_path.write_text(html, encoding="utf-8")
    print(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
