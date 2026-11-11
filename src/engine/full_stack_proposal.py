from __future__ import annotations

import argparse
import os
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import sqlalchemy as sa

from engine.capacity_proposal import (
    CAPACITY_INITIAL_CLAIM_ID,
    CAPACITY_RECOVERY_CLAIM_ID,
    EligibleFixtureReader,
    capacity_proposal_settings,
    generate_capacity_proposal,
    validate_capacity_recovery,
    validate_capacity_run,
)
from engine.db import create_engine
from engine.full_stack_worker import WorkerSettings
from engine.gate_ledger import PostgresGateLedger
from engine.odoo import OdooReader
from engine.policy import Gate, GateConfig
from engine.proposal import ProposalService
from engine.proposal_provider import OpenAIClient
from engine.retrieval import (
    MODEL_FILES,
    MODEL_ID,
    MODEL_REPOSITORY,
    MODEL_REVISION,
    LocalE5Embedder,
    PostgresRetriever,
)


@contextmanager
def progress(stage: str) -> Iterator[None]:
    stop = threading.Event()
    start = time.monotonic()

    def heartbeat() -> None:
        while not stop.wait(30):
            print(f"stage={stage} elapsed_s={time.monotonic() - start:.0f}", flush=True)

    print(f"stage={stage} status=started", flush=True)
    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one authorized capacity proposal")
    parser.add_argument("--run-id", required=True, type=uuid.UUID)
    parser.add_argument("--attempt", choices=("initial", "recovery"), default="initial")
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args()
    claim_id = (
        CAPACITY_INITIAL_CLAIM_ID if args.attempt == "initial" else CAPACITY_RECOVERY_CLAIM_ID
    )
    print("stage=proposal-preflight status=started", flush=True)
    worker = WorkerSettings.from_env(os.environ)
    settings = capacity_proposal_settings(os.environ)
    gate_config = GateConfig.from_env(os.environ)
    provider = OpenAIClient(os.environ.get("OPENAI_API_KEY", ""))
    reader = OdooReader(worker.odoo_url)
    reader.verify_synthetic_readonly_role()
    from huggingface_hub import snapshot_download  # type: ignore[import-not-found]

    snapshot_download(
        repo_id=MODEL_REPOSITORY,
        revision=MODEL_REVISION,
        token=False,
        local_files_only=True,
        allow_patterns=MODEL_FILES,
    )
    engine = create_engine(worker.engine_url)
    try:
        with engine.connect() as conn:
            languages = set(
                conn.execute(
                    sa.text("SELECT DISTINCT language FROM policy_passages WHERE model_id=:model"),
                    {"model": MODEL_ID},
                ).scalars()
            )
            claimed = conn.execute(
                sa.text("SELECT 1 FROM capacity_proposal_claim WHERE id=:id"),
                {"id": claim_id},
            ).first()
        if claimed:
            raise ValueError("the issue 68 proposal attempt is already claimed")
        if args.attempt == "recovery":
            validate_capacity_recovery(engine, run_id=args.run_id)
        if not {"en", "ar"}.issubset(languages):
            raise ValueError("the pinned synthetic policy index must include English and Arabic")
        validate_capacity_run(engine, reader, run_id=args.run_id, instance_id=worker.instance_id)
        print(
            "stage=proposal-preflight status=passed; provider access and model inference "
            "were not checked",
            flush=True,
        )
        if args.preflight_only:
            return
        with progress("proposal-model-load"):
            retriever = PostgresRetriever(engine, LocalE5Embedder())
        service = ProposalService(
            engine,
            EligibleFixtureReader(reader),
            retriever,
            provider,
            Gate(gate_config, PostgresGateLedger(engine)),
            settings,
        )
        with progress("proposal-generation"):
            generate_capacity_proposal(
                engine,
                reader,
                service,
                run_id=args.run_id,
                instance_id=worker.instance_id,
                env=os.environ,
                claim_id=claim_id,
            )
        print(
            f"stage=proposal-generation status=complete attempt={args.attempt} "
            f"run_id={args.run_id}",
            flush=True,
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
