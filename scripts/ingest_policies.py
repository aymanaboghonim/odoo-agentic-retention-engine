from __future__ import annotations

import argparse
import importlib.util
import sys
import threading
import time
from pathlib import Path

import sqlalchemy as sa

from engine.config import EngineConfig
from engine.db import create_engine
from engine.retrieval import (
    MODEL_FILES,
    MODEL_REPOSITORY,
    MODEL_REVISION,
    LocalE5Embedder,
    ingest,
    load_policies,
)


def main() -> int:
    import os

    parser = argparse.ArgumentParser(description="Index the checked-in synthetic policy snapshot")
    parser.add_argument("--source", type=Path, default=Path("policies/synthetic.json"))
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args()
    started = time.monotonic()
    print("stage=preflight", flush=True)
    passages = load_policies(args.source)
    config = EngineConfig.from_env(os.environ)
    engine = create_engine(config.database_url)
    try:
        with engine.connect() as conn:
            conn.execute(sa.text("SELECT source_id FROM policy_passages LIMIT 1"))
        if importlib.util.find_spec("fastembed") is None:
            raise RuntimeError("Install the locked retrieval-benchmark dependency group")
        try:
            from huggingface_hub import snapshot_download  # type: ignore[import-not-found]
        except ImportError as error:
            raise RuntimeError("Install the locked retrieval-benchmark dependency group") from error
        snapshot_download(
            repo_id=MODEL_REPOSITORY,
            revision=MODEL_REVISION,
            token=False,
            local_files_only=True,
            allow_patterns=MODEL_FILES,
        )
        print(
            f"preflight=ok passages={len(passages)}; model inference and retrieval quality "
            "were not checked",
            flush=True,
        )
        if args.preflight_only:
            return 0
        stop = threading.Event()

        def heartbeat() -> None:
            while not stop.wait(30):
                print(
                    f"stage=embedding-and-index elapsed_s={time.monotonic() - started:.0f}",
                    flush=True,
                )

        thread = threading.Thread(target=heartbeat, daemon=True)
        print("stage=embedding-and-index", flush=True)
        thread.start()
        try:
            count = ingest(engine, LocalE5Embedder(), args.source)
        finally:
            stop.set()
            thread.join()
        print(
            f"stage=complete passages={count} elapsed_s={time.monotonic() - started:.0f}",
            flush=True,
        )
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
