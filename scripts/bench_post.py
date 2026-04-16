#!/usr/bin/env python3
"""
Post-process benchmark outputs: manifest JSON, aggregated TSV, readable Markdown.

Invoked by scripts/bench-micro.sh and scripts/bench-macro.sh. Plotting: scripts/plot_bench_results.py.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def _run_capture(args: list[str], *, cwd: Path | None = None, timeout: float = 30.0) -> str:
    try:
        r = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if r.returncode != 0:
        return ""
    return r.stdout.strip()


def _git_head(cwd: Path | None) -> str:
    return _run_capture(["git", "rev-parse", "HEAD"], cwd=cwd)


def _git_dirty_line_count(cwd: Path | None) -> str:
    out = _run_capture(["git", "status", "--porcelain"], cwd=cwd)
    if not out:
        return "0"
    return str(len(out.splitlines()))


def _uname_line() -> str:
    u = platform.uname()
    return f"{u.system} {u.node} {u.release} {u.version} {u.machine}"


def _nproc_str() -> str:
    n = _run_capture(["nproc"])
    if n.isdigit():
        return n
    import os

    c = os.cpu_count()
    return str(c if c is not None else 0)


def _gcc_version_line() -> str:
    return _run_capture(["gcc", "--version"]).split("\n", 1)[0].strip()


def cmd_manifest(args: argparse.Namespace) -> int:
    path = Path(args.manifest_path).resolve()
    omp = {k: v for k, v in os.environ.items() if k.startswith("OMP_")}
    repo = path.parent
    while repo != repo.parent and not (repo / ".git").is_dir():
        repo = repo.parent
    git_cwd = repo if (repo / ".git").is_dir() else None

    manifest = {
        "schema": "shardingsim-bench-manifest-1",
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run_id": args.run_id,
        "suite": args.suite,
        "warmup_per_config": args.warmup,
        "reps_per_config": args.reps,
        "raw_tsv": args.raw_tsv,
        "default_counts_documentation": args.counts_doc.strip(),
        "git_commit": _git_head(git_cwd),
        "git_dirty_lines": _git_dirty_line_count(git_cwd),
        "uname": _uname_line(),
        "hostname": socket.gethostname(),
        "nproc": _nproc_str(),
        "gcc_version": _gcc_version_line(),
        "env_omp": omp,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {path}", file=sys.stderr)
    return 0


GROUP_KEYS = [
    "suite",
    "count",
    "threads",
    "batch",
    "mode",
    "inproc_sleep_ms",
    "recv_sleep_ms",
    "recv_verify",
]
NUM_FIELDS = [
    "wall_ms",
    "throughput_tx_s",
    "sum_build_ms",
    "sum_pack_ms",
    "sum_submit_ack_ms",
    "sum_inproc_ms",
    "bench_oneway_ms",
    "submitted",
    "batches",
]


def _float(s: str | None) -> float | None:
    if s is None or s == "" or str(s).upper() == "NA":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _quartiles(vals: list[float]) -> tuple[float | None, float | None, float | None]:
    if not vals:
        return None, None, None
    if len(vals) == 1:
        v = vals[0]
        return v, v, v
    xs = sorted(vals)
    n = len(xs)

    def q(p: float) -> float:
        idx = p * (n - 1)
        lo = int(idx)
        hi = min(lo + 1, n - 1)
        frac = idx - lo
        return xs[lo] * (1 - frac) + xs[hi] * frac

    return q(0.25), q(0.5), q(0.75)


def agg_path_for_raw(raw: Path) -> Path:
    name = raw.name
    if name.startswith("micro-raw-"):
        return raw.with_name("micro-agg-" + name.removeprefix("micro-raw-"))
    if name.startswith("macro-raw-"):
        return raw.with_name("macro-agg-" + name.removeprefix("macro-raw-"))
    return raw.with_name(raw.stem + "-agg.tsv")


def cmd_aggregate(args: argparse.Namespace) -> int:
    raw = Path(args.raw_tsv).resolve()
    if not raw.is_file():
        print(f"Not a file: {raw}", file=sys.stderr)
        return 1
    out = agg_path_for_raw(raw)
    rows: list[dict[str, str]] = []
    with raw.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f, delimiter="\t"):
            rows.append(dict(r))
    if not rows:
        print(f"Empty: {raw}", file=sys.stderr)
        return 1
    for r in rows:
        r.setdefault("suite", "")
        r.setdefault("rep", "1")

    def gkey(r: dict[str, str]) -> tuple[str, ...]:
        return tuple(r.get(k, "") for k in GROUP_KEYS)

    buckets: dict[tuple[str, ...], list[dict[str, str]]] = {}
    for r in rows:
        buckets.setdefault(gkey(r), []).append(r)

    out_fields = list(GROUP_KEYS) + ["n_total", "n_ok", "n_fail"]
    for nf in NUM_FIELDS:
        out_fields.extend([f"{nf}_q25", f"{nf}_median", f"{nf}_q75"])
    out_fields.extend(["host", "nproc"])

    out_rows: list[dict[str, str]] = []
    for key in sorted(buckets.keys()):
        grp = buckets[key]
        n_total = len(grp)
        n_ok = sum(1 for r in grp if r.get("exit_code", "").strip() == "0")
        n_fail = n_total - n_ok
        row = dict(zip(GROUP_KEYS, key))
        row["n_total"] = str(n_total)
        row["n_ok"] = str(n_ok)
        row["n_fail"] = str(n_fail)
        ok_rows = [r for r in grp if r.get("exit_code", "").strip() == "0"]
        for nf in NUM_FIELDS:
            vals = [_float(r.get(nf)) for r in ok_rows]
            vals = [v for v in vals if v is not None]
            q25, med, q75 = _quartiles(vals)
            row[f"{nf}_q25"] = "" if q25 is None else f"{q25:.6f}"
            row[f"{nf}_median"] = "" if med is None else f"{med:.6f}"
            row[f"{nf}_q75"] = "" if q75 is None else f"{q75:.6f}"
        row["host"] = grp[0].get("host", "")
        row["nproc"] = grp[0].get("nproc", "")
        out_rows.append(row)

    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=out_fields, delimiter="\t", extrasaction="ignore")
        w.writeheader()
        w.writerows(out_rows)
    print(f"Wrote {out} ({len(out_rows)} groups from {len(rows)} raw rows)", file=sys.stderr)
    return 0


GLOSSARY = """
| Term | Meaning |
|------|---------|
| **TX count** | How many transactions the generator submitted in that run. |
| **Threads** | OpenMP threads in the generator. |
| **Batch** | Transactions per batch. |
| **Mode** | `inproc` = no ZMQ; `zmq` = REQ/REP to the receiver. |
| **Verify** | Receiver runs `transaction_verify()` on each TX (`Yes` / `No`). |
| **Throughput** | TX/s from generator wall time. |
| **Wall (ms)** | Elapsed time for the benchmarked parallel region. |
| **One-way (ms)** | ZMQ: avg send-stamp → after `zmq_recv`. |
| **Runs OK / Total** | Successful reps vs total. |
"""


def _int_field(s: str | None, default: int = 0) -> int:
    if s is None or s == "" or str(s).upper() == "NA":
        return default
    try:
        return int(s, 10)
    except ValueError:
        return default


def _fmt_f(n: float | None, nd: int = 2) -> str:
    if n is None:
        return "—"
    return f"{n:,.{nd}f}"


def _verify_label(r: dict[str, str]) -> str:
    if r.get("mode") != "zmq":
        return "—"
    return "Yes" if (r.get("recv_verify") or "").strip() == "1" else "No"


def _sort_key_agg(r: dict[str, str]) -> tuple[str, int, int, int, int]:
    return (
        r.get("mode", ""),
        _int_field(r.get("count")),
        _int_field(r.get("threads")),
        _int_field(r.get("batch")),
        _int_field(r.get("recv_verify")),
    )


def _is_agg_row(row: dict[str, str]) -> bool:
    return bool((row.get("throughput_tx_s_median") or "").strip())


def _out_readable(inp: Path) -> Path:
    name = inp.name
    if name.startswith("micro-agg-"):
        return inp.with_name(
            "micro-readable-" + name.removeprefix("micro-agg-").removesuffix(".tsv") + ".md"
        )
    if name.startswith("micro-raw-"):
        return inp.with_name(
            "micro-readable-" + name.removeprefix("micro-raw-").removesuffix(".tsv") + ".md"
        )
    if name.startswith("macro-agg-"):
        return inp.with_name(
            "macro-readable-" + name.removeprefix("macro-agg-").removesuffix(".tsv") + ".md"
        )
    if name.startswith("macro-raw-"):
        return inp.with_name(
            "macro-readable-" + name.removeprefix("macro-raw-").removesuffix(".tsv") + ".md"
        )
    return inp.with_suffix(".readable.md")


def cmd_readable(args: argparse.Namespace) -> int:
    inp = Path(args.source_tsv).resolve()
    if not inp.is_file():
        print(f"Not a file: {inp}", file=sys.stderr)
        return 1
    agg: Path | None = None
    if inp.name.startswith("micro-raw-"):
        p = inp.with_name("micro-agg-" + inp.name.removeprefix("micro-raw-"))
        if p.is_file():
            agg = p
    elif inp.name.startswith("macro-raw-"):
        p = inp.with_name("macro-agg-" + inp.name.removeprefix("macro-raw-"))
        if p.is_file():
            agg = p
    data_path = agg if agg else inp
    with data_path.open(newline="", encoding="utf-8") as df:
        rows = list(csv.DictReader(df, delimiter="\t"))
    if not rows:
        print(f"Empty: {data_path}", file=sys.stderr)
        return 1
    note = f"(used `{agg.name}`)\n**Raw:** `{inp.name}`" if agg else ""

    bench_label = "Macro" if (inp.name.startswith("macro-") or (rows[0].get("suite") or "").startswith("macro-")) else "Micro"

    if _is_agg_row(rows[0]):
        rows.sort(key=_sort_key_agg)
        n_fail = sum(1 for r in rows if (r.get("n_fail") or "0").strip() not in ("", "0"))
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        lines = [
            f"# {bench_label} benchmark — readable summary",
            "",
            f"**Generated:** {now}",
            f"**Source:** `{data_path.name}`" + (f" {note}" if note else ""),
            f"**Configurations:** {len(rows)} · **All OK:** {'yes' if n_fail == 0 else f'no ({n_fail} with failures)'}",
            "",
            "## What the numbers mean",
            "",
            GLOSSARY.strip(),
            "",
            "## Highlights (highest throughput, median)",
            "",
        ]
        by_mode: dict[str, list[dict[str, str]]] = {}
        for r in rows:
            by_mode.setdefault(r.get("mode", "?"), []).append(r)

        def top5(sub: list[dict[str, str]]) -> list[dict[str, str]]:
            scored: list[tuple[float, dict[str, str]]] = []
            for r in sub:
                tp = _float(r.get("throughput_tx_s_median"))
                if tp is not None and (r.get("n_fail") or "0").strip() == "0":
                    scored.append((tp, r))
            scored.sort(key=lambda x: -x[0])
            return [x[1] for x in scored[:5]]

        for mode, sub in sorted(by_mode.items()):
            lines += [
                f"### `{mode}`",
                "",
                "| # | TX | Thr | Batch | Verify | Throughput (tx/s) | Wall (ms) |",
                "|---:|---:|---:|---:|:---:|---:|---:|",
            ]
            for i, r in enumerate(top5(sub), 1):
                lines.append(
                    f"| {i} | {r.get('count', '')} | {r.get('threads', '')} | {r.get('batch', '')} | {_verify_label(r)} | "
                    f"{_fmt_f(_float(r.get('throughput_tx_s_median')), 0)} | {_fmt_f(_float(r.get('wall_ms_median')), 2)} |"
                )
            lines.append("")

        lines += ["## Full results by mode", ""]

        def emit_table(title: str, sub: list[dict[str, str]]) -> None:
            lines.append(f"### {title}")
            lines.append("")
            lines.append(
                "| TX | Thr | Batch | Verify | Throughput med (tx/s) | Wall med (ms) | One-way med (ms) | OK runs |"
            )
            lines.append("|---:|---:|---:|:---:|---:|---:|---:|---:|")
            for r in sub:
                ow = (r.get("bench_oneway_ms_median") or "").strip()
                ow_disp = _fmt_f(_float(ow), 3) if ow and ow.upper() != "NA" else "—"
                lines.append(
                    f"| {r.get('count', '')} | {r.get('threads', '')} | {r.get('batch', '')} | {_verify_label(r)} | "
                    f"{_fmt_f(_float(r.get('throughput_tx_s_median')), 0)} | {_fmt_f(_float(r.get('wall_ms_median')), 2)} | {ow_disp} | "
                    f"{r.get('n_ok', '')}/{r.get('n_total', '')} |"
                )
            lines.append("")

        inproc = [r for r in rows if r.get("mode") == "inproc"]
        zmq_nv = [r for r in rows if r.get("mode") == "zmq" and _int_field(r.get("recv_verify")) != 1]
        zmq_v = [r for r in rows if r.get("mode") == "zmq" and _int_field(r.get("recv_verify")) == 1]
        if inproc:
            emit_table("In-process (no ZMQ)", inproc)
        if zmq_nv:
            emit_table("ZMQ · verify **off**", zmq_nv)
        if zmq_v:
            emit_table("ZMQ · verify **on**", zmq_v)
        agg_glob = "macro-agg-*.tsv" if bench_label == "Macro" else "micro-agg-*.tsv"
        body = "\n".join(lines) + f"\n*See `{agg_glob}` or HTML report for quartiles.*\n"
    else:
        for r in rows:
            r.setdefault("suite", "")
            r.setdefault("rep", "1")
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        fails = sum(1 for r in rows if r.get("exit_code", "").strip() != "0")
        rb = "Macro" if inp.name.startswith("macro-") else "Micro"
        lines = [
            f"# {rb} benchmark — readable (raw)",
            "",
            f"**Generated:** {now}",
            f"**Source:** `{inp.name}`",
            f"**Rows:** {len(rows)} · **Failed:** {fails}",
            "",
            GLOSSARY.strip(),
            "",
            "## All runs",
            "",
            "| Suite | Rep | TX | Thr | Batch | Mode | Verify | OK | Throughput | Wall (ms) | One-way |",
            "|:---|---:|---:|---:|---:|:---|:---|:---:|---:|---:|---|",
        ]
        for r in rows:
            ok = "✓" if r.get("exit_code", "").strip() == "0" else "✗"
            ow = (r.get("bench_oneway_ms") or "").strip()
            ow_disp = _fmt_f(_float(ow), 3) if ow and ow.upper() != "NA" else "—"
            lines.append(
                f"| {r.get('suite', '')} | {r.get('rep', '')} | {r.get('count', '')} | {r.get('threads', '')} | "
                f"{r.get('batch', '')} | {r.get('mode', '')} | {_verify_label(r)} | {ok} | "
                f"{_fmt_f(_float(r.get('throughput_tx_s')), 0)} | {_fmt_f(_float(r.get('wall_ms')), 2)} | {ow_disp} |"
            )
        body = "\n".join(lines) + "\n"

    out = _out_readable(inp)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(body, encoding="utf-8")
    print(f"Wrote {out}", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Benchmark post-processing (manifest, aggregate, readable).")
    sub = p.add_subparsers(dest="command", required=True)

    pm = sub.add_parser("manifest", help="Write JSON manifest beside the benchmark run.")
    pm.add_argument("manifest_path", type=Path, help="Output JSON path")
    pm.add_argument("run_id", help="Run identifier (timestamp string)")
    pm.add_argument("suite", help="Suite name: quick, standard, or full")
    pm.add_argument("warmup", type=int, help="Warmup repetitions per config")
    pm.add_argument("reps", type=int, help="Recorded repetitions per config")
    pm.add_argument("raw_tsv", help="Path to micro-raw-*.tsv or macro-raw-*.tsv (may not exist yet)")
    pm.add_argument("counts_doc", help="Human-readable description of count/thread/batch grid")
    pm.set_defaults(func=cmd_manifest)

    pa = sub.add_parser("aggregate", help="Group raw TSV rows and write *-agg-*.tsv next to raw")
    pa.add_argument("raw_tsv", help="Input micro-raw-*.tsv or macro-raw-*.tsv")
    pa.set_defaults(func=cmd_aggregate)

    pr = sub.add_parser("readable", help="Write *-readable-*.md from raw or aggregate TSV")
    pr.add_argument(
        "source_tsv",
        type=Path,
        help="micro-raw-*.tsv or macro-raw-*.tsv (prefers sibling *-agg-*.tsv if present)",
    )
    pr.set_defaults(func=cmd_readable)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
