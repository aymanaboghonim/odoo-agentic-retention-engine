UV ?= uv
UV_VERSION ?= 0.13.5
BASE_REF ?= origin/main
CI_PULL_REQUEST ?= 0
GIT_BRANCH ?=
PR_BODY ?=

export BASE_REF CI_PULL_REQUEST GIT_BRANCH PR_BODY

.PHONY: setup ci verify-uv preflight-s3 smoke-s3 benchmark-s3

setup:
	@command -v $(UV) >/dev/null || { echo "Install uv 0.13.5 before setup"; exit 1; }
	@test "$$($(UV) --version | awk '{print $$2}')" = "$(UV_VERSION)" || { echo "Expected uv $(UV_VERSION)"; exit 1; }
	@command -v docker >/dev/null || { echo "Docker is required for the link and secret checks"; exit 1; }
	$(UV) sync --locked
	$(UV) run pre-commit install

ci:
	@command -v $(UV) >/dev/null || { echo "Install uv 0.13.5 before running CI"; exit 1; }
	@test "$$($(UV) --version | awk '{print $$2}')" = "$(UV_VERSION)" || { echo "Expected uv $(UV_VERSION)"; exit 1; }
	@command -v docker >/dev/null || { echo "Docker is required for the link and secret checks"; exit 1; }
	$(UV) sync --locked
	$(UV) run --no-sync pre-commit run --all-files
	$(UV) run --no-sync python scripts/check_traceability.py

verify-uv:
	@command -v $(UV) >/dev/null || { echo "Install uv $(UV_VERSION) before running S3 retrieval commands"; exit 1; }
	@test "$$($(UV) --version | awk '{print $$2}')" = "$(UV_VERSION)" || { echo "Expected uv $(UV_VERSION)"; exit 1; }

preflight-s3: verify-uv
	bash scripts/run_s3_benchmark.sh --preflight-only

smoke-s3: verify-uv
	bash scripts/run_s3_benchmark.sh --smoke-only

benchmark-s3: verify-uv
	bash scripts/run_s3_benchmark.sh
