#!/usr/bin/env bash
set -euo pipefail

preflight_only=0
smoke_only=0
case "${1:-}" in
  --preflight-only)
    preflight_only=1
    ;;
  --smoke-only)
    smoke_only=1
    ;;
  "")
    ;;
  *)
    echo "Usage: $0 [--preflight-only|--smoke-only]" >&2
    exit 2
    ;;
esac
if (($# > 0)); then
  shift
fi
if (($# > 0)); then
  echo "Usage: $0 [--preflight-only|--smoke-only]" >&2
  exit 2
fi

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
run_markdown_path="${run_report_path}.md"

monitor_dir=""
monitor_pid=""
monitor_stop_file=""
compose_started=0

run_stage() {
  local stage="$1"
  shift
  local started=$SECONDS
  local command_pid sleep_pid finished_pid status=0

  printf 'progress stage=%s status=started elapsed_seconds=0\n' "$stage" >&2
  "$@" &
  command_pid=$!
  while true; do
    sleep 30 &
    sleep_pid=$!
    finished_pid=""
    wait -n -p finished_pid "$command_pid" "$sleep_pid" || status=$?
    if [[ "$finished_pid" == "$command_pid" ]]; then
      kill "$sleep_pid" 2>/dev/null || true
      wait "$sleep_pid" 2>/dev/null || true
      break
    fi
    if [[ "$finished_pid" == "$sleep_pid" ]]; then
      printf 'progress stage=%s status=heartbeat elapsed_seconds=%d\n' \
        "$stage" "$((SECONDS - started))" >&2
    fi
  done
  if ((status == 0)); then
    printf 'progress stage=%s status=completed elapsed_seconds=%d\n' \
      "$stage" "$((SECONDS - started))" >&2
  else
    printf 'progress stage=%s status=failed exit_code=%d elapsed_seconds=%d\n' \
      "$stage" "$status" "$((SECONDS - started))" >&2
  fi
  return "$status"
}

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
  if ((compose_started)); then
    if ! run_stage cleanup_compose "${compose[@]}" down --volumes; then
      echo "Failed to remove the S3 benchmark Compose project" >&2
      if ((status == 0)); then
        status=1
      fi
    fi
    compose_started=0
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
  if [[ -e "$run_markdown_path" ]]; then
    rm -- "$run_markdown_path" || status=1
  fi
  exit "$status"
}
trap cleanup EXIT

run_stage preflight uv run --no-sync python scripts/s3_retrieval_benchmark.py \
  --dataset evals/retrieval/s3_dataset.json --report "$run_report_path" --preflight
if ((preflight_only)); then
  exit 0
fi

compose_started=1
run_stage docker_startup "${compose[@]}" up --detach --wait pgvector qdrant
run_stage runner_image_build "${compose[@]}" build benchmark
run_stage backend_smoke "${compose[@]}" run --rm --no-deps benchmark --smoke
if ((smoke_only)); then
  exit 0
fi

monitor_dir="$(mktemp -d)"
monitor_stop_file="$monitor_dir/stop"
uv run --no-sync python -m scripts.monitor_s3_benchmark_memory \
  --report "$run_report_path" \
  --stop-file "$monitor_stop_file" &
monitor_pid=$!

benchmark_status=0
run_stage benchmark "${compose[@]}" run --rm --no-deps benchmark \
  --report "/reports/$run_report_name" \
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
if [[ ! -s "$run_markdown_path" ]]; then
  echo "Benchmark completed without a Markdown summary" >&2
  exit 1
fi
run_stage cleanup_compose "${compose[@]}" down --volumes
compose_started=0
run_stage publish_reports uv run --no-sync python scripts/s3_retrieval_benchmark.py \
  --publish-staged-report "$run_report_path" --report "$final_report_path"
