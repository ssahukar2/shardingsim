#!/usr/bin/env bash
#
# Micro benchmark driver: generator grid → raw TSV → micro_post.py (manifest, aggregate,
# readable) → plot_micro_results.py → micro-agg-*.html. README § Benchmarks for overview.
#
# Suite: BENCH_MICRO_SUITE=quick|standard|full, or BENCH_MICRO_QUICK=1 / BENCH_MICRO_FULL=1.
# Overrides: BENCH_MICRO_COUNTS, BENCH_MICRO_THREADS, BENCH_MICRO_BATCHES (space-separated),
#   BENCH_MICRO_REPS, BENCH_MICRO_WARMUP, BENCH_MICRO_OUT.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

MICRO_POST="${SCRIPT_DIR}/micro_post.py"
require_micro_post() {
  [[ -f "$MICRO_POST" ]] || {
    echo "Missing post-processor: $MICRO_POST" >&2
    exit 1
  }
}
run_micro_post() {
  python3 "$MICRO_POST" "$@"
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

# Every thread count 1..cap (inclusive), capped by nproc
micro_threads_seq() {
  local cap=$1
  local m=$NPROC
  ((m > cap)) && m=$cap
  ((m < 1)) && m=1
  seq 1 "$m"
}

SUITE="${BENCH_MICRO_SUITE:-standard}"
[[ "${BENCH_MICRO_QUICK:-0}" == "1" ]] && SUITE="quick"
[[ "${BENCH_MICRO_FULL:-0}" == "1" && "${BENCH_MICRO_QUICK:-0}" != "1" ]] && SUITE="full"

ZMQ_ONEWAY=0
COUNTS_DOC=""

if [[ "$SUITE" == "quick" ]]; then
  COUNTS=(256 512 1024)
  THREADS=(1)
  BATCHES=(32)
  INPROC_SLEEPS=(0)
  ZMQ_CFG=("0|0")
  ZMQ_ONEWAY=0
  COUNTS_DOC="${COUNTS[*]}"
elif [[ "$SUITE" == "standard" ]]; then
  ZMQ_ONEWAY=1
  # Default grid (128–1024 TX): balanced counts × threads 1,2,4,8,12 (capped by nproc) × batches 8–128
  if [[ -n "${BENCH_MICRO_COUNTS:-}" ]]; then
    read -r -a COUNTS <<< "${BENCH_MICRO_COUNTS}"
  else
    COUNTS=(128 256 384 512 768 1024)
  fi
  if [[ -n "${BENCH_MICRO_THREADS:-}" ]]; then
    read -r -a THREADS <<< "${BENCH_MICRO_THREADS}"
  else
    THREADS=()
    for t in 1 2 4 8 12; do
      ((t <= NPROC)) && THREADS+=("$t")
    done
    [[ ${#THREADS[@]} -eq 0 ]] && THREADS=(1)
  fi
  if [[ -n "${BENCH_MICRO_BATCHES:-}" ]]; then
    read -r -a BATCHES <<< "${BENCH_MICRO_BATCHES}"
  else
    BATCHES=(8 16 32 64 128)
  fi
  INPROC_SLEEPS=(0)
  ZMQ_CFG=("0|0" "0|1")
  COUNTS_DOC="${COUNTS[*]} threads=${THREADS[*]} batches=${BATCHES[*]}"
else
  ZMQ_ONEWAY=1
  if [[ -n "${BENCH_MICRO_COUNTS:-}" ]]; then
    read -r -a COUNTS <<< "${BENCH_MICRO_COUNTS}"
  else
    COUNTS=(32 48 64 96 128 192 256 384 512 768 1024 1536 2048 3072 4096 6144 8192)
  fi
  if [[ -n "${BENCH_MICRO_THREADS:-}" ]]; then
    read -r -a THREADS <<< "${BENCH_MICRO_THREADS}"
  else
    mapfile -t THREADS < <(micro_threads_seq 20)
  fi
  if [[ -n "${BENCH_MICRO_BATCHES:-}" ]]; then
    read -r -a BATCHES <<< "${BENCH_MICRO_BATCHES}"
  else
    BATCHES=(8 16 24 32 48 64 96 128 192 256 384)
  fi
  INPROC_SLEEPS=(0)
  ZMQ_CFG=("0|0" "0|1")
  COUNTS_DOC="${COUNTS[*]} threads=${THREADS[*]} batches=${BATCHES[*]}"
fi

if [[ "$SUITE" == "quick" ]]; then
  WARMUP="${BENCH_MICRO_WARMUP:-0}"
  REPS="${BENCH_MICRO_REPS:-1}"
else
  WARMUP="${BENCH_MICRO_WARMUP:-1}"
  REPS="${BENCH_MICRO_REPS:-3}"
fi

if [[ -n "${BENCH_MICRO_OUT:-}" ]]; then
  OUT="${BENCH_MICRO_OUT}"
else
  OUT="results/micro-raw-${RUN_ID}.tsv"
fi
mkdir -p "$(dirname "$OUT")"

if [[ -n "${BENCH_MICRO_OUT:-}" ]]; then
  MANIFEST="$(dirname "$OUT")/$(basename "$OUT" .tsv)-manifest.json"
else
  MANIFEST="results/micro-manifest-${RUN_ID}.json"
fi

if command -v python3 >/dev/null 2>&1; then
  require_micro_post
  run_micro_post manifest "$MANIFEST" "$RUN_ID" "$SUITE" "$WARMUP" "$REPS" "$OUT" "$COUNTS_DOC"
else
  echo "python3 required for manifest/aggregate/readable; skipping manifest" >&2
fi

{
  echo -ne "suite\trep\tcount\tthreads\tbatch\tmode\tinproc_sleep_ms\trecv_sleep_ms\trecv_verify\t"
  echo -ne "exit_code\twall_ms\tsubmitted\tbatches\tsum_build_ms\tsum_pack_ms\tsum_submit_ack_ms\tsum_inproc_ms\tzmq\tthroughput_tx_s\tbench_oneway_ms\thost\tnproc\tunix_ts\n"
} >"$OUT"

MODES_PER=$((1 + ${#ZMQ_CFG[@]}))
CELLS=$(( ${#COUNTS[@]} * ${#THREADS[@]} * ${#BATCHES[@]} * MODES_PER ))
RUNS_EST=$(( CELLS * (WARMUP + REPS) ))
echo >&2 "Coverage: ${#COUNTS[@]} counts × ${#THREADS[@]} threads × ${#BATCHES[@]} batches × ${MODES_PER} modes (inproc + ${#ZMQ_CFG[@]} zmq) = ${CELLS} configs → ~${RUNS_EST} generator runs (warmup=${WARMUP} reps=${REPS} each)."

RUN=0
run_one() {
  local count=$1 threads=$2 batch=$3 mode=$4 ips=$5 rs=$6 rv=$7 rep=$8 do_append=$9
  local tmp exit_code=0
  tmp=$(mktemp)
  ((RUN++)) || true
  printf '\r[micro %d] suite=%s rep=%s count=%s thr=%s batch=%s mode=%s   ' \
    "$RUN" "$SUITE" "$rep" "$count" "$threads" "$batch" "$mode" >&2 || true

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
    echo -ne "${SUITE}\t${rep}\t${count}\t${threads}\t${batch}\t${mode}\t${ips}\t${rs}\t${rv}\t" >>"$OUT"
    echo -ne "${exit_code}\t${log}\t${oneway}\t${HOST}\t${NPROC}\t$(date +%s)\n" >>"$OUT"
  fi
}

for c in "${COUNTS[@]}"; do
  for t in "${THREADS[@]}"; do
    for b in "${BATCHES[@]}"; do
      for ips in "${INPROC_SLEEPS[@]}"; do
        for ((w = 1; w <= WARMUP; w++)); do
          run_one "$c" "$t" "$b" "inproc" "$ips" "NA" "NA" "warmup" 0
        done
        for ((r = 1; r <= REPS; r++)); do
          run_one "$c" "$t" "$b" "inproc" "$ips" "NA" "NA" "$r" 1
        done
      done
      for cfg in "${ZMQ_CFG[@]}"; do
        IFS='|' read -r rs rv <<<"$cfg"
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

echo "" >&2
echo "Micro benchmarks done (suite=$SUITE): $RUN executions -> $OUT (warmup=$WARMUP reps=$REPS per config)" >&2

AGG_OUT="${OUT/micro-raw-/micro-agg-}"
HTML_OUT="${AGG_OUT%.tsv}.html"
if command -v python3 >/dev/null 2>&1; then
  require_micro_post
  run_micro_post aggregate "$OUT" || true
  if [[ -f "$AGG_OUT" ]]; then
    run_micro_post readable "$OUT" || true
    PLOT_PY="${SCRIPT_DIR}/plot_micro_results.py"
    if [[ -f "$PLOT_PY" ]]; then
      python3 "$PLOT_PY" "$AGG_OUT" -o "$HTML_OUT" || true
      echo "HTML report: $HTML_OUT" >&2
    fi
  fi
else
  echo "python3 missing; skipped aggregate + readable + html" >&2
fi
