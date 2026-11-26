from __future__ import annotations

import ipaddress
import os
import re
import secrets
import uuid
from collections.abc import Callable
from typing import Literal

import sqlalchemy as sa
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from engine import telemetry
from engine.auth import AuthError, AuthSettings, Principal, TokenVerifier
from engine.config import EngineConfig
from engine.db import ApprovalConflict, RunRepository, approval_requests, create_engine
from engine.runtime_state import (
    RuntimeState,
    RuntimeStateConflict,
    RuntimeStateError,
    RuntimeStateRepository,
)

ORIGIN_HEADER = "x-origin-verify"
# Liveness for the container health check: outside the edge prefix (CloudFront forwards only
# `/api/*`) and answered only to loopback clients, without authentication or data.
HEALTH_PATH = "/healthz"
# INF-9: optional path prefix that the edge forwards (CloudFront sends `/api/*` unchanged).
ROOT_PATH_PATTERN = re.compile(r"^(/[a-z0-9][a-z0-9-]{0,30}){1,3}$")


def validate_root_path(value: str | None) -> str:
    """Return a normalized route prefix; empty means routes at `/` (default, unchanged)."""
    root_path = (value or "").strip()
    if root_path and not ROOT_PATH_PATTERN.fullmatch(root_path):
        raise ValueError(
            "APPROVAL_API_ROOT_PATH must look like /api (lowercase, no trailing slash)"
        )
    return root_path


class DecisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Literal["approve", "reject"]


class RuntimeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: Literal["active", "fallback_fixed_rule", "halted"]
    reason: str = Field(min_length=3, max_length=200)
    expected_version: int | None = Field(default=None, ge=1)


def _is_loopback(request: Request) -> bool:
    try:
        return request.client is not None and ipaddress.ip_address(request.client.host).is_loopback
    except ValueError:
        return False


def create_app(
    repo: RunRepository,
    verifier: TokenVerifier,
    *,
    origin_verify_secret: str | None = None,
    runtime: RuntimeStateRepository | None = None,
    root_path: str = "",
) -> FastAPI:
    """Approval API: behind CloudFront with an origin secret, otherwise loopback only."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    router = APIRouter()
    prefix = validate_root_path(root_path)
    expected_origin = origin_verify_secret.encode("ascii") if origin_verify_secret else None

    @app.middleware("http")
    async def origin_and_cache(request: Request, call_next):  # type: ignore[no-untyped-def]
        if request.url.path == HEALTH_PATH:
            if not _is_loopback(request):
                return JSONResponse(status_code=404, content={"detail": "not found"})
            response = await call_next(request)
            response.headers["Cache-Control"] = "no-store"
            return response
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

    @router.get("/approvals")
    def list_approvals(
        principal: Principal = Depends(require_role("viewer")),
    ) -> list[dict[str, object]]:
        return repo.pending_approvals()

    @router.post("/approvals/{run_id}/decision")
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
        # OBS-15 (#128): the decision code joins the run's trace; never the operator identity.
        with telemetry.span(
            "aorte.approval",
            run_id=run_id,
            **{
                "aorte.run_id": str(run_id),
                "aorte.decision": payload.decision,
                "langfuse.observation.type": "event",
                "langfuse.trace.name": "aorte.run",
            },
        ):
            pass
        return {"run_id": str(run.id), "state": run.state.value, "decision": payload.decision}

    if runtime is not None:

        @router.get("/runtime")
        def read_runtime(
            principal: Principal = Depends(require_role("viewer")),
        ) -> dict[str, object]:
            return runtime.get().as_dict()

        @router.post("/runtime/state")
        def change_runtime(
            payload: RuntimeInput, principal: Principal = Depends(require_role("admin"))
        ) -> dict[str, object]:
            # KS-2: admin only. The actor is the token sub; the reason is validated as non-PII.
            try:
                snapshot = runtime.set_state(
                    RuntimeState(payload.state),
                    actor=principal.subject,
                    reason=payload.reason,
                    expected_version=payload.expected_version,
                )
            except RuntimeStateConflict as error:
                raise HTTPException(
                    status_code=409, detail="runtime state changed or is already set"
                ) from error
            except RuntimeStateError as error:
                raise HTTPException(
                    status_code=422, detail="invalid runtime state change"
                ) from error
            return snapshot.as_dict()

    @app.get(HEALTH_PATH, include_in_schema=False)
    def health() -> JSONResponse:
        try:
            repo.ping()
        except Exception:  # noqa: BLE001 - report unavailable, never the reason
            return JSONResponse(status_code=503, content={"status": "unavailable"})
        return JSONResponse(status_code=200, content={"status": "ok"})

    app.include_router(router, prefix=prefix)
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
    root_path = validate_root_path(os.environ.get("APPROVAL_API_ROOT_PATH"))
    tel = telemetry.configure_from_env(os.environ, service_name="aorte-approval-api")  # OBS-6
    engine = create_engine(config.database_url)
    try:
        with engine.connect() as conn:
            conn.execute(sa.select(approval_requests.c.run_id).limit(0))
        app = create_app(
            RunRepository(engine),
            verifier,
            origin_verify_secret=settings.origin_verify_secret,
            runtime=RuntimeStateRepository(engine),
            root_path=root_path,
        )
        telemetry.instrument_fastapi(app)  # #134: no-op unless TELEMETRY_APM=YES
        uvicorn.run(
            app,
            host=host,
            port=port,
            proxy_headers=False,
            access_log=False,
        )
    finally:
        engine.dispose()
        tel.shutdown()


if __name__ == "__main__":
    main()
