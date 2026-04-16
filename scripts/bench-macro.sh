#!/usr/bin/env bash
#
# Macro benchmark driver: sustained end-to-end generator → ZMQ receiver (large tx counts,
# optional simulated receiver delay). Same TSV schema as micro; outputs macro-raw-*.tsv →
# bench_post.py (manifest | aggregate | readable) → plot_bench_results.py → macro-agg-*.html
#
# Focus: throughput under realistic per-batch receiver work (sleep_ms) and verify on/off.
# Default grid is ZMQ-only; set BENCH_MACRO_INPROC=1 to include in-process baseline per cell.
#
# Suite: BENCH_MACRO_SUITE=quick|standard|full, or BENCH_MACRO_QUICK=1 / BENCH_MACRO_FULL=1
# Overrides: BENCH_MACRO_COUNTS, BENCH_MACRO_THREADS, BENCH_MACRO_BATCHES,
#   BENCH_MACRO_RECV_SLEEPS, BENCH_MACRO_REPS, BENCH_MACRO_WARMUP, BENCH_MACRO_OUT, BENCH_MACRO_INPROC
#
# Default grids (ZMQ × verify on/off; omit inproc unless BENCH_MACRO_INPROC=1):
#   quick    — 100k TX · 8 threads · batch 64 · recv 0 ms · verify off (smoke).
#   standard — TX: 50k–1M (6 steps); threads: 1,2,4,8,12,16,24,32,48,64 (≤ nproc);
#              batches: 32–256; recv ms: 0,1,2,5,10; verify 0/1. Tuned for bare metal (e.g. Chameleon).
#   full     — TX: 50k–2M (9 steps); threads: 1,2,4,6,8,12,16,24,32,48,64 (≤ nproc);
#              batches: 16–512; recv ms: 0,1,2,3,5,8,10; verify 0/1. Very large — use REPS=1 to scout.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

BENCH_POST="${SCRIPT_DIR}/bench_post.py"
require_bench_post() {
  [[ -f "$BENCH_POST" ]] || {
    echo "Missing post-processor: $BENCH_POST" >&2
    exit 1
  }
}
run_bench_post() {
  python3 "$BENCH_POST" "$@"
}

GEN=./build/generator
[[ -x "$GEN" ]] || { echo "Run \`make\` first; missing $GEN" >&2; exit 1; }

NPROC=$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4)
HOST=$(hostname -s 2>/dev/null || hostname)
RUN_ID=$(date +%Y%m%d-%H%M%S)

parse_bench_line() {
  local log=$1
  local line
  line=$(grep '^BENCH_LINE ' "$log" 2>/dev/null | tail -1 || true)
  if [[ -z "$line" ]]; then
    printf '%s' $'NA\tNA\tNA\tNA\tNA\tNA\tNA\tNA\tNA'
    return
  fi
  echo "$line" | awk '{
    delete a
    for (i = 2; i <= NF; i++) {
      eq = index($i, "=")
      key = substr($i, 1, eq - 1)
      val = substr($i, eq + 1)
      a[key] = val
    }
    printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s",
      a["wall_ms"], a["submitted"], a["batches"], a["sum_build_ms"], a["sum_pack_ms"],
      a["sum_submit_ack_ms"], a["sum_inproc_ms"], a["zmq"], a["throughput_tx_s"]
  }'
}

parse_oneway_avg() {
  local log=$1
  awk -F= '/^bench_oneway_ms=/ { s += $2; n++ } END { if (n > 0) printf "%.6f", s/n; else print "NA" }' "$log" 2>/dev/null || echo "NA"
}

SUITE="${BENCH_MACRO_SUITE:-standard}"
[[ "${BENCH_MACRO_QUICK:-0}" == "1" ]] && SUITE="quick"
[[ "${BENCH_MACRO_FULL:-0}" == "1" && "${BENCH_MACRO_QUICK:-0}" != "1" ]] && SUITE="full"

ZMQ_ONEWAY=1
INCLUDE_INPROC="${BENCH_MACRO_INPROC:-0}"
COUNTS_DOC=""

if [[ "$SUITE" == "quick" ]]; then
  if [[ -n "${BENCH_MACRO_COUNTS:-}" ]]; then
    read -r -a COUNTS <<< "${BENCH_MACRO_COUNTS}"
  else
    COUNTS=(100000)
  fi
  if [[ -n "${BENCH_MACRO_THREADS:-}" ]]; then
    read -r -a THREADS <<< "${BENCH_MACRO_THREADS}"
  else
    THREADS=(8)
  fi
  if [[ -n "${BENCH_MACRO_BATCHES:-}" ]]; then
    read -r -a BATCHES <<< "${BENCH_MACRO_BATCHES}"
  else
    BATCHES=(64)
  fi
  if [[ -n "${BENCH_MACRO_RECV_SLEEPS:-}" ]]; then
    read -r -a RECV_SLEEPS <<< "${BENCH_MACRO_RECV_SLEEPS}"
  else
    RECV_SLEEPS=(0)
  fi
  VERIFY_VALUES=(0)
  WARMUP="${BENCH_MACRO_WARMUP:-0}"
  REPS="${BENCH_MACRO_REPS:-1}"
elif [[ "$SUITE" == "standard" ]]; then
  if [[ -n "${BENCH_MACRO_COUNTS:-}" ]]; then
    read -r -a COUNTS <<< "${BENCH_MACRO_COUNTS}"
  else
    COUNTS=(50000 100000 200000 400000 600000 1000000)
  fi
  if [[ -n "${BENCH_MACRO_THREADS:-}" ]]; then
    read -r -a THREADS <<< "${BENCH_MACRO_THREADS}"
  else
    THREADS=()
    for t in 1 2 4 8 12 16 24 32 48 64; do
      ((t <= NPROC)) && THREADS+=("$t")
    done
    [[ ${#THREADS[@]} -eq 0 ]] && THREADS=(1)
  fi
  if [[ -n "${BENCH_MACRO_BATCHES:-}" ]]; then
    read -r -a BATCHES <<< "${BENCH_MACRO_BATCHES}"
  else
    BATCHES=(32 64 128 256)
  fi
  if [[ -n "${BENCH_MACRO_RECV_SLEEPS:-}" ]]; then
    read -r -a RECV_SLEEPS <<< "${BENCH_MACRO_RECV_SLEEPS}"
  else
    RECV_SLEEPS=(0 1 2 5 10)
  fi
  VERIFY_VALUES=(0 1)
  WARMUP="${BENCH_MACRO_WARMUP:-1}"
  REPS="${BENCH_MACRO_REPS:-3}"
else
  if [[ -n "${BENCH_MACRO_COUNTS:-}" ]]; then
    read -r -a COUNTS <<< "${BENCH_MACRO_COUNTS}"
  else
    COUNTS=(50000 100000 200000 300000 500000 750000 1000000 1500000 2000000)
  fi
  if [[ -n "${BENCH_MACRO_THREADS:-}" ]]; then
    read -r -a THREADS <<< "${BENCH_MACRO_THREADS}"
  else
    THREADS=()
    for t in 1 2 4 6 8 12 16 24 32 48 64; do
      ((t <= NPROC)) && THREADS+=("$t")
    done
    [[ ${#THREADS[@]} -eq 0 ]] && THREADS=(1)
  fi
  if [[ -n "${BENCH_MACRO_BATCHES:-}" ]]; then
    read -r -a BATCHES <<< "${BENCH_MACRO_BATCHES}"
  else
    BATCHES=(16 32 64 128 256 512)
  fi
  if [[ -n "${BENCH_MACRO_RECV_SLEEPS:-}" ]]; then
    read -r -a RECV_SLEEPS <<< "${BENCH_MACRO_RECV_SLEEPS}"
  else
    RECV_SLEEPS=(0 1 2 3 5 8 10)
  fi
  VERIFY_VALUES=(0 1)
  WARMUP="${BENCH_MACRO_WARMUP:-1}"
  REPS="${BENCH_MACRO_REPS:-3}"
fi

SUITE_TAG="macro-${SUITE}"

ZMQ_VARIANTS=$((${#RECV_SLEEPS[@]} * ${#VERIFY_VALUES[@]}))
INPROC_VARIANTS=0
[[ "$INCLUDE_INPROC" == "1" ]] && INPROC_VARIANTS=1
MODES_PER=$((INPROC_VARIANTS + ZMQ_VARIANTS))

COUNTS_DOC="${COUNTS[*]} threads=${THREADS[*]} batches=${BATCHES[*]} recv_ms=${RECV_SLEEPS[*]} zmq_variants=${ZMQ_VARIANTS} inproc=${INCLUDE_INPROC}"

if [[ -n "${BENCH_MACRO_OUT:-}" ]]; then
  OUT="${BENCH_MACRO_OUT}"
else
  OUT="results/macro-raw-${RUN_ID}.tsv"
fi
mkdir -p "$(dirname "$OUT")"

if [[ -n "${BENCH_MACRO_OUT:-}" ]]; then
  MANIFEST="$(dirname "$OUT")/$(basename "$OUT" .tsv)-manifest.json"
else
  MANIFEST="results/macro-manifest-${RUN_ID}.json"
fi

if command -v python3 >/dev/null 2>&1; then
  require_bench_post
  run_bench_post manifest "$MANIFEST" "$RUN_ID" "$SUITE_TAG" "$WARMUP" "$REPS" "$OUT" "$COUNTS_DOC"
else
  echo "python3 required for manifest/aggregate/readable; skipping manifest" >&2
fi

{
  echo -ne "suite\trep\tcount\tthreads\tbatch\tmode\tinproc_sleep_ms\trecv_sleep_ms\trecv_verify\t"
  echo -ne "exit_code\twall_ms\tsubmitted\tbatches\tsum_build_ms\tsum_pack_ms\tsum_submit_ack_ms\tsum_inproc_ms\tzmq\tthroughput_tx_s\tbench_oneway_ms\thost\tnproc\tunix_ts\n"
} >"$OUT"

CELLS=$(( ${#COUNTS[@]} * ${#THREADS[@]} * ${#BATCHES[@]} * MODES_PER ))
RUNS_EST=$(( CELLS * (WARMUP + REPS) ))
echo >&2 "Macro coverage: ${#COUNTS[@]} counts × ${#THREADS[@]} threads × ${#BATCHES[@]} batches × ${MODES_PER} modes (inproc=${INCLUDE_INPROC}, zmq recv×verify=${ZMQ_VARIANTS}) = ${CELLS} configs → ~${RUNS_EST} generator runs (warmup=${WARMUP} reps=${REPS} each)."

RUN=0
run_one() {
  local count=$1 threads=$2 batch=$3 mode=$4 ips=$5 rs=$6 rv=$7 rep=$8 do_append=$9
  local tmp exit_code=0
  tmp=$(mktemp)
  ((RUN++)) || true
  printf '\r[macro %d] suite=%s rep=%s count=%s thr=%s batch=%s mode=%s recv=%s verify=%s   ' \
    "$RUN" "$SUITE_TAG" "$rep" "$count" "$threads" "$batch" "$mode" "$rs" "$rv" >&2 || true

  local oneway="NA"

  if [[ "$mode" == "inproc" ]]; then
    set +e
    "$GEN" alice bob 1 "$count" --in-process --threads "$threads" --batch "$batch" \
      --sleep-ms "$ips" --bench &>"$tmp"
    exit_code=$?
    set -e
  else
    local -a ra=(--receiver-arg --sleep-ms --receiver-arg "$rs")
    [[ "$rv" == "1" ]] && ra+=(--receiver-arg --verify)
    ra+=(--receiver-arg --bench)
    local -a zextra=()
    if [[ "$ZMQ_ONEWAY" == "1" ]]; then
      ra+=(--receiver-arg --bench-oneway)
      zextra+=(--bench-oneway)
    fi
    set +e
    "$GEN" alice bob 1 "$count" --threads "$threads" --batch "$batch" --bench \
      "${zextra[@]}" --spawn-receiver "${ra[@]}" &>"$tmp"
    exit_code=$?
    set -e
    oneway=$(parse_oneway_avg "$tmp")
  fi

  local log
  log=$(parse_bench_line "$tmp")
  rm -f "$tmp"

  if [[ "$do_append" == "1" ]]; then
    echo -ne "${SUITE_TAG}\t${rep}\t${count}\t${threads}\t${batch}\t${mode}\t${ips}\t${rs}\t${rv}\t" >>"$OUT"
    echo -ne "${exit_code}\t${log}\t${oneway}\t${HOST}\t${NPROC}\t$(date +%s)\n" >>"$OUT"
  fi
}

for c in "${COUNTS[@]}"; do
  for t in "${THREADS[@]}"; do
    for b in "${BATCHES[@]}"; do
      if [[ "$INCLUDE_INPROC" == "1" ]]; then
        for ((w = 1; w <= WARMUP; w++)); do
          run_one "$c" "$t" "$b" "inproc" "0" "NA" "NA" "warmup" 0
        done
        for ((r = 1; r <= REPS; r++)); do
          run_one "$c" "$t" "$b" "inproc" "0" "NA" "NA" "$r" 1
        done
      fi
      for rs in "${RECV_SLEEPS[@]}"; do
        for rv in "${VERIFY_VALUES[@]}"; do
          for ((w = 1; w <= WARMUP; w++)); do
            run_one "$c" "$t" "$b" "zmq" "NA" "$rs" "$rv" "warmup" 0
          done
          for ((r = 1; r <= REPS; r++)); do
            run_one "$c" "$t" "$b" "zmq" "NA" "$rs" "$rv" "$r" 1
          done
        done
      done
    done
  done
done

echo "" >&2
echo "Macro benchmarks done (suite=$SUITE_TAG): $RUN executions -> $OUT (warmup=$WARMUP reps=$REPS per config)" >&2

AGG_OUT="${OUT/macro-raw-/macro-agg-}"
HTML_OUT="${AGG_OUT%.tsv}.html"
if command -v python3 >/dev/null 2>&1; then
  require_bench_post
  run_bench_post aggregate "$OUT" || true
  if [[ -f "$AGG_OUT" ]]; then
    run_bench_post readable "$OUT" || true
    PLOT_PY="${SCRIPT_DIR}/plot_bench_results.py"
    if [[ -f "$PLOT_PY" ]]; then
      python3 "$PLOT_PY" "$AGG_OUT" -o "$HTML_OUT" || true
      echo "HTML report: $HTML_OUT" >&2
    fi
  fi
else
  echo "python3 missing; skipped aggregate + readable + html" >&2
fi
