UV ?= uv
UV_VERSION ?= 0.13.5
BASE_REF ?= origin/main
CI_PULL_REQUEST ?= 0
GIT_BRANCH ?=
PR_BODY ?=

export BASE_REF CI_PULL_REQUEST GIT_BRANCH PR_BODY

.PHONY: setup ci

setup:
	@command -v $(UV) >/dev/null || { echo "Install uv 0.13.5 before setup"; exit 1; }
	@test "$$($(UV) --version | awk '{print $$2}')" = "$(UV_VERSION)" || { echo "Expected uv $(UV_VERSION)"; exit 1; }
	@command -v docker >/dev/null || { echo "Docker is required for the link and secret checks"; exit 1; }
	$(UV) sync --locked --all-groups
	$(UV) run pre-commit install

ci:
	@command -v $(UV) >/dev/null || { echo "Install uv 0.13.5 before running CI"; exit 1; }
	@test "$$($(UV) --version | awk '{print $$2}')" = "$(UV_VERSION)" || { echo "Expected uv $(UV_VERSION)"; exit 1; }
	@command -v docker >/dev/null || { echo "Docker is required for the link and secret checks"; exit 1; }
	$(UV) sync --locked --all-groups
	$(UV) run --no-sync pre-commit run --all-files
	$(UV) run --no-sync python scripts/check_traceability.py
