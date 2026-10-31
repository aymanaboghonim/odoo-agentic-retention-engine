#!/usr/bin/env bash
set -euo pipefail

compose=(
  docker compose
  --project-name s3-retrieval-benchmark
  --file deploy/s3-benchmark-compose.yml
)
report_dir="$(pwd)/evals/retrieval/results"
final_report_path="$report_dir/s3-benchmark.json"
export S3_BENCHMARK_REPORT_DIR="$report_dir"
export S3_BENCHMARK_HF_CACHE_DIR="${HF_HUB_CACHE:-${HF_HOME:-${HOME}/.cache/huggingface}/hub}"
export S3_BENCHMARK_UID="$(id -u)"
export S3_BENCHMARK_GID="$(id -g)"
mkdir -p "$report_dir" "$S3_BENCHMARK_HF_CACHE_DIR"
run_report_path="$(mktemp "$report_dir/.s3-benchmark.XXXXXX")"
run_report_name="$(basename "$run_report_path")"

monitor_dir=""
monitor_pid=""
monitor_stop_file=""

cleanup() {
  local status=$?
  if [[ -n "$monitor_pid" ]]; then
    touch "$monitor_stop_file" || status=1
    wait "$monitor_pid" || {
      local monitor_status=$?
      if ((status == 0)); then
        status=$monitor_status
      fi
    }
    monitor_pid=""
  fi
  if ! "${compose[@]}" down --volumes; then
    echo "Failed to remove the S3 benchmark Compose project" >&2
    if ((status == 0)); then
      status=1
    fi
  fi
  if [[ -n "$monitor_stop_file" && -e "$monitor_stop_file" ]]; then
    rm -- "$monitor_stop_file" || status=1
  fi
  if [[ -n "$monitor_dir" && -d "$monitor_dir" ]]; then
    rmdir -- "$monitor_dir" || status=1
  fi
  if [[ -e "$run_report_path" ]]; then
    rm -- "$run_report_path" || status=1
  fi
  exit "$status"
}
trap cleanup EXIT

"${compose[@]}" up --detach --wait pgvector qdrant
"${compose[@]}" build benchmark

monitor_dir="$(mktemp -d)"
monitor_stop_file="$monitor_dir/stop"
uv run --no-sync python -m scripts.monitor_s3_benchmark_memory \
  --report "$run_report_path" \
  --stop-file "$monitor_stop_file" &
monitor_pid=$!

benchmark_status=0
"${compose[@]}" run --rm --no-deps benchmark --report "/reports/$run_report_name" \
  || benchmark_status=$?
touch "$monitor_stop_file"
monitor_status=0
wait "$monitor_pid" || monitor_status=$?
monitor_pid=""

if ((benchmark_status != 0)); then
  exit "$benchmark_status"
fi
if ((monitor_status != 0)); then
  exit "$monitor_status"
fi
if [[ ! -s "$run_report_path" ]]; then
  echo "Benchmark completed without a report" >&2
  exit 1
fi
mv -f -- "$run_report_path" "$final_report_path"
