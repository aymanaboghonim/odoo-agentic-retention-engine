from __future__ import annotations

import ipaddress
import os
import secrets
import uuid
from collections.abc import Callable
from typing import Literal

import sqlalchemy as sa
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from engine.auth import AuthError, AuthSettings, Principal, TokenVerifier
from engine.config import EngineConfig
from engine.db import ApprovalConflict, RunRepository, approval_requests, create_engine

ORIGIN_HEADER = "x-origin-verify"


class DecisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Literal["approve", "reject"]


def _is_loopback(request: Request) -> bool:
    try:
        return request.client is not None and ipaddress.ip_address(request.client.host).is_loopback
    except ValueError:
        return False


def create_app(
    repo: RunRepository, verifier: TokenVerifier, *, origin_verify_secret: str | None = None
) -> FastAPI:
    """Approval API: behind CloudFront with an origin secret, otherwise loopback only."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    expected_origin = origin_verify_secret.encode("ascii") if origin_verify_secret else None

    @app.middleware("http")
    async def origin_and_cache(request: Request, call_next):  # type: ignore[no-untyped-def]
        # Runs before authentication (RBAC-5): the request must come through the trusted edge.
        if expected_origin is not None:
            supplied = request.headers.get(ORIGIN_HEADER, "").encode("utf-8", "replace")
            if not secrets.compare_digest(supplied, expected_origin):
                return JSONResponse(status_code=403, content={"detail": "forbidden"})
        elif not _is_loopback(request):
            return JSONResponse(status_code=403, content={"detail": "local access only"})
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    def authenticate(authorization: str | None = Header(default=None)) -> Principal:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(
                status_code=401,
                detail="invalid credentials",
                headers={"WWW-Authenticate": "Bearer"},
            )
        try:
            return verifier.verify(authorization.removeprefix("Bearer "))
        except AuthError:
            raise HTTPException(
                status_code=401,
                detail="invalid credentials",
                headers={"WWW-Authenticate": "Bearer"},
            ) from None

    def require_role(role: str) -> Callable[[Principal], Principal]:
        def check(principal: Principal = Depends(authenticate)) -> Principal:
            if not principal.has(role):
                raise HTTPException(status_code=403, detail="insufficient role")
            return principal

        return check

    @app.get("/approvals")
    def list_approvals(
        principal: Principal = Depends(require_role("viewer")),
    ) -> list[dict[str, object]]:
        return repo.pending_approvals()

    @app.post("/approvals/{run_id}/decision")
    def decide(
        run_id: uuid.UUID,
        payload: DecisionInput,
        principal: Principal = Depends(require_role("approver")),
    ) -> dict[str, str]:
        try:
            run = repo.decide_approval(
                run_id,
                token=payload.snapshot_hash,
                decision=payload.decision,
                actor=principal.subject,
            )
        except ApprovalConflict as error:
            raise HTTPException(
                status_code=409, detail="approval is stale or unavailable"
            ) from error
        return {"run_id": str(run.id), "state": run.state.value, "decision": payload.decision}

    return app


def _host(settings: AuthSettings) -> str:
    host = os.environ.get("APPROVAL_API_HOST", "127.0.0.1").strip() or "127.0.0.1"
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if not loopback and settings.origin_verify_secret is None:
        raise ValueError("a non-loopback APPROVAL_API_HOST requires APPROVAL_ORIGIN_VERIFY_SECRET")
    return host


def main() -> None:
    import uvicorn

    settings = AuthSettings.from_env(os.environ)
    verifier = TokenVerifier(settings)
    config = EngineConfig.from_env(os.environ)
    port = int(os.environ.get("APPROVAL_API_PORT", "8080"))
    if not 1 <= port <= 65535:
        raise ValueError("APPROVAL_API_PORT must be a valid port")
    host = _host(settings)
    engine = create_engine(config.database_url)
    try:
        with engine.connect() as conn:
            conn.execute(sa.select(approval_requests.c.run_id).limit(0))
        uvicorn.run(
            create_app(
                RunRepository(engine),
                verifier,
                origin_verify_secret=settings.origin_verify_secret,
            ),
            host=host,
            port=port,
            proxy_headers=False,
            access_log=False,
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
