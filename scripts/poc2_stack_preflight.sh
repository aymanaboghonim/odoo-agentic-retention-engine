#!/usr/bin/env bash

poc2_stack_preflight() {
  if [[ ! -f "$root/.env" || "$(stat -c '%a' "$root/.env")" != "600" ]]; then
    echo "The isolated stack .env must exist and have mode 0600" >&2
    return 1
  fi
  echo "stage=stack-preflight status=started"
  local network status service
  network="$(docker network inspect poc68-isolated_private \
    --format '{{.Internal}} {{index .Labels "com.docker.compose.project"}}')"
  if [[ "$network" != "true poc68-isolated" ]]; then
    echo "The dedicated internal Compose network is required" >&2
    return 1
  fi
  for service in odoo-db odoo engine-db worker approval-api mailpit; do
    status="$(docker inspect "poc68-isolated-${service}-1" \
      --format '{{.State.Running}} {{index .Config.Labels "com.docker.compose.project"}} {{index .Config.Labels "com.docker.compose.service"}}')"
    if [[ "$status" != "true poc68-isolated $service" ]]; then
      echo "Expected isolated $service container is not running" >&2
      return 1
    fi
  done
  docker image inspect poc68-isolated-proposal:latest >/dev/null
  compose=(docker compose --project-name poc68-isolated --env-file "$root/.env"
    -f "$root/deploy/full-stack-compose.yml")
  "${compose[@]}" config --quiet
  echo "stage=stack-preflight status=passed"
}
