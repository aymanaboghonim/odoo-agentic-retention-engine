from __future__ import annotations

import pytest

from engine.config import ConfigError, EngineConfig
from engine.runs import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATES,
    InvalidTransition,
    RunState,
    validate_transition,
)


def test_run_rejects_invalid_state_transitions() -> None:
    with pytest.raises(InvalidTransition):
        validate_transition(RunState.RECEIVED, RunState.EXECUTING)
    with pytest.raises(InvalidTransition):
        validate_transition(RunState.AWAITING_APPROVAL, RunState.EXECUTING)
    validate_transition(RunState.RECEIVED, RunState.VALIDATING_INPUT)


def test_terminal_states_cannot_return_to_active_states() -> None:
    assert TERMINAL_STATES == {
        RunState.COMPLETED,
        RunState.REJECTED,
        RunState.CANCELLED,
        RunState.FAILED,
    }
    for terminal in TERMINAL_STATES:
        for target in RunState:
            with pytest.raises(InvalidTransition):
                validate_transition(terminal, target)


def test_every_active_state_can_fail() -> None:
    for state, nxt in ALLOWED_TRANSITIONS.items():
        if state not in TERMINAL_STATES:
            assert RunState.FAILED in nxt


def test_missing_config_fails_closed() -> None:
    with pytest.raises(ConfigError):
        EngineConfig.from_env({})
    with pytest.raises(ConfigError):
        EngineConfig.from_env({"ENGINE_DATABASE_URL": "  "})
    with pytest.raises(ConfigError):
        EngineConfig.from_env({"ENGINE_DATABASE_URL": "mysql://x/y"})


def test_config_normalizes_postgres_url() -> None:
    cfg = EngineConfig.from_env({"ENGINE_DATABASE_URL": "postgresql://u:p@h:5432/d"})
    assert cfg.database_url == "postgresql+psycopg://u:p@h:5432/d"
