from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path
from typing import Sequence

from scripts.s3_retrieval_benchmark import parse_docker_stats

CONTAINER_NAMES = (
    "s3-retrieval-benchmark-pgvector-1",
    "s3-retrieval-benchmark-qdrant-1",
)


def sample_container_memory_max(stop_file: Path) -> dict[str, float]:
    maximums: dict[str, float] = {}
    while not stop_file.exists():
        result = subprocess.run(
            [
                "docker",
                "stats",
                *CONTAINER_NAMES,
                "--no-stream",
                "--format",
                "{{.Name}}\t{{.MemUsage}}",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        current = parse_docker_stats(result.stdout)
        if set(current) != set(CONTAINER_NAMES):
            raise RuntimeError(f"Docker stats missed a benchmark container: {current}")
        for name, memory in current.items():
            maximums[name] = max(memory, maximums.get(name, 0.0))
        time.sleep(1)
    if set(maximums) != set(CONTAINER_NAMES):
        raise RuntimeError(f"Memory monitor collected incomplete data: {maximums}")
    return {name: round(value, 1) for name, value in sorted(maximums.items())}


def update_report_memory(report_path: Path, maximums: dict[str, float]) -> None:
    if set(maximums) != set(CONTAINER_NAMES):
        raise ValueError(f"Expected memory for both benchmark containers, got {maximums}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        raise ValueError("Benchmark report must be a JSON object")
    if "container_memory_max_mib" in report:
        raise ValueError("Benchmark report already contains container memory measurements")
    report["container_memory_max_mib"] = maximums
    report_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sample S3 benchmark container memory")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--stop-file", type=Path, required=True)
    arguments = parser.parse_args(argv)
    maximums = sample_container_memory_max(arguments.stop_file)
    update_report_memory(arguments.report, maximums)
    print(json.dumps(maximums, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
