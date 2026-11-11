from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import statistics
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

PROJECT = "poc68-isolated"
COMPOSE = Path(__file__).resolve().parents[1] / "deploy/full-stack-compose.yml"
UNITS = {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}
SAMPLE_INTERVAL_SECONDS = 1.0
STAGE_TIMEOUT_SECONDS = 1200


def command(args: list[str], *, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(args, text=True, capture_output=True, env=env, check=False)
    if result.returncode:
        raise RuntimeError(f"{args[0]} failed with exit status {result.returncode}")
    return result.stdout


def bytes_value(value: str) -> int:
    match = re.fullmatch(r"\s*([\d.]+)\s*(B|KiB|MiB|GiB)\s*", value)
    if not match:
        raise ValueError("Docker returned an unknown memory unit")
    return int(float(match.group(1)) * UNITS[match.group(2)])


def snapshot() -> dict[str, Any]:
    ids = command(
        ["docker", "ps", "-q", "--filter", f"label=com.docker.compose.project={PROJECT}"]
    ).split()
    if not ids:
        raise RuntimeError("no running isolated stack containers")
    try:
        output = command(["docker", "stats", "--no-stream", "--format", "{{json .}}", *ids])
    except RuntimeError:
        refreshed_ids = command(
            ["docker", "ps", "-q", "--filter", f"label=com.docker.compose.project={PROJECT}"]
        ).split()
        if not refreshed_ids or refreshed_ids == ids:
            raise
        ids = refreshed_ids
        output = command(["docker", "stats", "--no-stream", "--format", "{{json .}}", *ids])
    containers = []
    for line in output.splitlines():
        row = json.loads(line)
        name = row["Name"]
        if not name.startswith(PROJECT + "-"):
            raise RuntimeError("unexpected container in capacity sample")
        containers.append(
            {
                "name": name,
                "cpu_percent": float(row["CPUPerc"].removesuffix("%")),
                "memory_bytes": bytes_value(row["MemUsage"].split("/")[0]),
            }
        )
    if len(containers) != len(ids):
        raise RuntimeError("container set changed while sampling")
    return {"at": time.time(), "containers": containers}


def preflight(compose: list[str], output: Path, run_id: uuid.UUID, attempt: str) -> None:
    if not output.is_dir() or not os.access(output, os.W_OK):
        raise ValueError("output directory must already exist and be writable")
    if not shutil.which("docker"):
        raise ValueError("docker is required")
    if not os.environ.get("OPENAI_API_KEY"):
        raise ValueError("OPENAI_API_KEY is required for the authorized live proposal")
    network = json.loads(command(["docker", "network", "inspect", PROJECT + "_private"]))[0]
    if not network["Internal"] or network["Labels"].get("com.docker.compose.project") != PROJECT:
        raise ValueError("the stack must use its own internal Compose network")
    running = command(
        [
            "docker",
            "ps",
            "--filter",
            f"label=com.docker.compose.project={PROJECT}",
            "--format",
            '{{.Label "com.docker.compose.service"}}',
        ]
    ).splitlines()
    required = {"odoo-db", "odoo", "engine-db", "worker", "approval-api", "mailpit"}
    if set(running) != required:
        raise ValueError("expected isolated services are not all running")
    command(compose + ["config", "--quiet"])
    command(
        compose
        + [
            "run",
            "--rm",
            "--no-deps",
            "-e",
            "FULL_STACK_CAPACITY_PROPOSAL=YES",
            "proposal",
            "python",
            "-m",
            "engine.full_stack_proposal",
            "--attempt",
            attempt,
            "--run-id",
            str(run_id),
            "--preflight-only",
        ]
    )
    print(
        f"preflight=passed attempt={attempt}; provider access, model inference and "
        "production capacity were not checked",
        flush=True,
    )


def totals(samples: list[dict[str, Any]]) -> dict[str, float]:
    cpu = [sum(row["cpu_percent"] for row in sample["containers"]) for sample in samples]
    memory = [sum(row["memory_bytes"] for row in sample["containers"]) for sample in samples]
    return {
        "mean_cpu_percent": statistics.mean(cpu),
        "max_cpu_percent": max(cpu),
        "mean_memory_bytes": statistics.mean(memory),
        "max_memory_bytes": max(memory),
    }


def measure_stage(stage: str, command_args: list[str]) -> tuple[list[dict[str, Any]], float]:
    samples = [snapshot()]
    started = time.monotonic()
    last_progress = started
    with tempfile.TemporaryFile(mode="w+t") as log:
        process = subprocess.Popen(command_args, stdout=log, stderr=subprocess.STDOUT)
        try:
            while process.poll() is None:
                elapsed = time.monotonic() - started
                if elapsed >= STAGE_TIMEOUT_SECONDS:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    raise TimeoutError(f"{stage} exceeded its {STAGE_TIMEOUT_SECONDS}s bound")
                samples.append(snapshot())
                elapsed = time.monotonic() - started
                if elapsed - last_progress >= 30:
                    print(
                        f"stage={stage} status=running elapsed_s={elapsed:.0f} "
                        f"samples={len(samples)}",
                        flush=True,
                    )
                    last_progress = elapsed
                time.sleep(SAMPLE_INTERVAL_SECONDS)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        samples.append(snapshot())
    elapsed = time.monotonic() - started
    if process.returncode:
        raise RuntimeError(f"{stage} failed with exit status {process.returncode}")
    return samples, elapsed


def publish(parent: Path, report: dict[str, Any]) -> Path:
    name = f"issue68-{report['run_id']}"
    destination = parent / name
    if destination.exists():
        raise FileExistsError("a report for this run already exists")
    with tempfile.TemporaryDirectory(prefix=".issue68-", dir=parent) as temporary:
        path = Path(temporary)
        raw = json.dumps(report, indent=2, sort_keys=True) + "\n"
        (path / "raw.json").write_text(raw, encoding="utf-8")
        idle = report["summary"]["idle"]
        scan = report["summary"]["scan"]
        proposal = report["summary"]["proposal"]
        (path / "README.md").write_text(
            "# Synthetic full-stack capacity measurement\n\n"
            f"- Run: `{report['run_id']}`\n"
            f"- Attempt: `{report['attempt']}`\n"
            f"- Host: {report['host']['platform']}; "
            f"logical CPUs: {report['host']['logical_cpus']}\n"
            f"- Idle samples: {len(report['idle_samples'])}; max CPU: "
            f"{idle['max_cpu_percent']:.2f}%; max memory: "
            f"{idle['max_memory_bytes'] / 1024**2:.1f} MiB\n"
            f"- Scan samples: {len(report['scan_samples'])}; max CPU: "
            f"{scan['max_cpu_percent']:.2f}%; max memory: "
            f"{scan['max_memory_bytes'] / 1024**2:.1f} MiB\n"
            f"- Proposal samples: {len(report['proposal_samples'])}; max CPU: "
            f"{proposal['max_cpu_percent']:.2f}%; max memory: "
            f"{proposal['max_memory_bytes'] / 1024**2:.1f} MiB\n"
            f"- Stage durations: scan {report['stage_elapsed_seconds']['scan']:.2f}s; "
            f"proposal {report['stage_elapsed_seconds']['proposal']:.2f}s\n"
            "- CPU is the sum of Docker per-container percentages (100% is one CPU); memory "
            "is the sum of Docker-reported per-container working-set values. Samples are "
            "sequential snapshots with a one-second pause plus Docker command runtime, "
            "not simultaneous peaks.\n"
            "- The bounded scan and at most one authorized proposal were measured in separate "
            "sequential stages. No message or coupon is sent.\n"
            "- This is not a throughput, production sizing, cost billing, or concurrency test. "
            "Token-count billing and active provider limits are unknown.\n"
            "- [Raw samples and provenance](raw.json)\n",
            encoding="utf-8",
        )
        os.rename(path, destination)
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description="Measure the isolated synthetic full stack")
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--run-id", type=uuid.UUID, required=True)
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument(
        "--recovery-after-sampler-failure",
        action="store_true",
        help="use the single owner-approved recovery claim after a settled initial call",
    )
    args = parser.parse_args()
    attempt = "recovery" if args.recovery_after_sampler_failure else "initial"
    if not args.env_file.is_file():
        raise ValueError("the isolated stack env file is missing")
    compose = [
        "docker",
        "compose",
        "-p",
        PROJECT,
        "--env-file",
        str(args.env_file),
        "-f",
        str(COMPOSE),
    ]
    print("stage=preflight status=started", flush=True)
    preflight(compose, args.output_dir, args.run_id, attempt)
    if args.preflight_only:
        return
    if (args.output_dir / f"issue68-{args.run_id}").exists():
        raise FileExistsError("a report for this run already exists")
    print("stage=idle status=started samples=3", flush=True)
    idle = []
    for number in range(3):
        idle.append(snapshot())
        print(f"stage=idle completed={number + 1}/3", flush=True)
    print("stage=scan status=started", flush=True)
    scan_samples, scan_elapsed = measure_stage(
        "scan",
        compose
        + [
            "run",
            "--rm",
            "--no-deps",
            "worker",
            "python",
            "-m",
            "engine.full_stack_worker",
            "--once",
        ],
    )
    print("stage=proposal status=started", flush=True)
    proposal_samples, proposal_elapsed = measure_stage(
        "proposal",
        compose
        + [
            "run",
            "--rm",
            "--no-deps",
            "-e",
            "FULL_STACK_CAPACITY_PROPOSAL=YES",
            "proposal",
            "python",
            "-m",
            "engine.full_stack_proposal",
            "--run-id",
            str(args.run_id),
            "--attempt",
            attempt,
        ],
    )
    report = {
        "run_id": str(args.run_id),
        "attempt": attempt,
        "host": {"platform": platform.platform(), "logical_cpus": os.cpu_count()},
        "method": (
            "sequential docker stats --no-stream snapshots with one-second pauses "
            "plus command runtime"
        ),
        "stage_elapsed_seconds": {"scan": scan_elapsed, "proposal": proposal_elapsed},
        "idle_samples": idle,
        "scan_samples": scan_samples,
        "proposal_samples": proposal_samples,
        "summary": {
            "idle": totals(idle),
            "scan": totals(scan_samples),
            "proposal": totals(proposal_samples),
        },
    }
    destination = publish(args.output_dir, report)
    print(f"stage=complete report={destination}", flush=True)


if __name__ == "__main__":
    main()
