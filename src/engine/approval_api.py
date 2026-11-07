from __future__ import annotations

import ipaddress
import os
import re
import secrets
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

import sqlalchemy as sa
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from engine.config import EngineConfig
from engine.db import ApprovalConflict, RunRepository, approval_requests, create_engine


@dataclass(frozen=True)
class Operator:
    name: str
    token: str = field(repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Operator:
        name = env.get("APPROVAL_OPERATOR", "")
        token = env.get("APPROVAL_TOKEN", "")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", name):
            raise ValueError("APPROVAL_OPERATOR must be a valid operator name")
        if len(token) < 32 or len(token) > 512 or not token.isascii() or not token.isprintable():
            raise ValueError("APPROVAL_TOKEN must be 32-512 printable ASCII characters")
        if any(c.isspace() for c in token):
            raise ValueError("APPROVAL_TOKEN must not contain whitespace")
        return cls(name, token)


class DecisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Literal["approve", "reject"]


def create_app(repo: RunRepository, operator: Operator) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def local_only(request: Request, call_next):  # type: ignore[no-untyped-def]
        try:
            local = (
                request.client is not None and ipaddress.ip_address(request.client.host).is_loopback
            )
        except ValueError:
            local = False
        if not local:
            return JSONResponse(status_code=403, content={"detail": "local access only"})
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    def authenticate(authorization: str | None = Header(default=None)) -> str:
        supplied = authorization.removeprefix("Bearer ") if authorization else ""
        if (
            not authorization
            or not authorization.startswith("Bearer ")
            or not secrets.compare_digest(supplied.encode("utf-8"), operator.token.encode("ascii"))
        ):
            raise HTTPException(status_code=401, detail="invalid credentials")
        return operator.name

    @app.get("/approvals")
    def list_approvals(actor: str = Depends(authenticate)) -> list[dict[str, object]]:
        return repo.pending_approvals()

    @app.post("/approvals/{run_id}/decision")
    def decide(
        run_id: uuid.UUID, payload: DecisionInput, actor: str = Depends(authenticate)
    ) -> dict[str, str]:
        try:
            run = repo.decide_approval(
                run_id, token=payload.snapshot_hash, decision=payload.decision, actor=actor
            )
        except ApprovalConflict as error:
            raise HTTPException(
                status_code=409, detail="approval is stale or unavailable"
            ) from error
        return {"run_id": str(run.id), "state": run.state.value, "decision": payload.decision}

    return app


def main() -> None:
    import uvicorn

    operator = Operator.from_env(os.environ)
    config = EngineConfig.from_env(os.environ)
    port = int(os.environ.get("APPROVAL_API_PORT", "8080"))
    if not 1 <= port <= 65535:
        raise ValueError("APPROVAL_API_PORT must be a valid port")
    engine = create_engine(config.database_url)
    try:
        with engine.connect() as conn:
            conn.execute(sa.select(approval_requests.c.run_id).limit(0))
        uvicorn.run(
            create_app(RunRepository(engine), operator),
            host="127.0.0.1",
            port=port,
            proxy_headers=False,
            access_log=False,
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
