from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


class ConfigError(Exception):
    """Raised when required engine configuration is missing or invalid."""


@dataclass(frozen=True)
class EngineConfig:
    database_url: str

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> EngineConfig:
        raw = env.get("ENGINE_DATABASE_URL", "").strip()
        if not raw:
            raise ConfigError("ENGINE_DATABASE_URL must be set.")
        return cls(database_url=normalize_database_url(raw))


def normalize_database_url(url: str) -> str:
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    if url.startswith("postgresql+psycopg://"):
        return url
    raise ConfigError("ENGINE_DATABASE_URL must be a PostgreSQL URL.")
