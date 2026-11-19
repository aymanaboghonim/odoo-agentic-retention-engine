"""Measure the OpenTelemetry SDK memory overhead of one engine process (#126, F-084).

Run once with telemetry off and once on, and compare the peak RSS:

    uv run --no-sync python scripts/measure_telemetry_memory.py off
    uv run --no-sync python scripts/measure_telemetry_memory.py on

`on` uses the real OTLP/HTTP exporters pointed at an unreachable loopback endpoint, so no
data leaves the machine and every export fails: the queues fill, which is the worst case.
This measures one Python process only, not the Compose or AWS containers.
"""

from __future__ import annotations

import argparse
import logging
import resource
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import engine.erp_cli  # noqa: E402,F401  same imports as the services
import engine.full_stack_worker  # noqa: E402,F401
from engine import telemetry  # noqa: E402

UNREACHABLE = {
    "TELEMETRY_ENABLED": "YES",
    "TELEMETRY_ENVIRONMENT": "local",
    "GRAFANA_OTLP_ENDPOINT": "https://127.0.0.1:9/otlp",
    "GRAFANA_OTLP_TOKEN": "synthetic-not-a-token",
    "TELEMETRY_DAILY_SPAN_CAP": "100000",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", choices=("off", "on"))
    parser.add_argument("--runs", type=int, default=2000, help="proposal-like span pairs")
    args = parser.parse_args(argv)
    env = UNREACHABLE if args.mode == "on" else {"TELEMETRY_ENABLED": "NO"}
    logging.basicConfig(level=logging.INFO, handlers=[logging.NullHandler()])
    handle = telemetry.configure_from_env(env, service_name="aorte-worker")
    log = logging.getLogger("engine.measure")
    print(f"stage=emit status=started mode={args.mode} runs={args.runs}", flush=True)
    for index in range(args.runs):
        with telemetry.span("aorte.proposal", **{"aorte.run_id": str(uuid.uuid4())}):
            with telemetry.span("aorte.llm.call", **{"gen_ai.usage.input_tokens": 1000}):
                pass
        telemetry.count("aorte.llm.tokens", 10, {"gen_ai.token.type": "input"})
        if index % 10 == 0:
            log.info("proposal run=%s status=validated", uuid.uuid4())
    handle.shutdown()
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024
    print(f"stage=emit status=completed mode={args.mode} peak_rss_mib={peak}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
