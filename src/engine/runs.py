from __future__ import annotations

from enum import StrEnum


class RunState(StrEnum):
    RECEIVED = "received"
    VALIDATING_INPUT = "validating_input"
    LOADING_CONTEXT = "loading_context"
    GENERATING_PROPOSAL = "generating_proposal"
    EVALUATING_POLICY = "evaluating_policy"
    AWAITING_APPROVAL = "awaiting_approval"
    READY_TO_EXECUTE = "ready_to_execute"
    EXECUTING = "executing"
    COMPLETED = "completed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    FAILED = "failed"


S = RunState

# Mirrors the transition table in docs/specs/agent-state-machine.md.
ALLOWED_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    S.RECEIVED: frozenset({S.VALIDATING_INPUT, S.CANCELLED, S.FAILED}),
    S.VALIDATING_INPUT: frozenset({S.LOADING_CONTEXT, S.REJECTED, S.FAILED}),
    S.LOADING_CONTEXT: frozenset({S.GENERATING_PROPOSAL, S.FAILED}),
    S.GENERATING_PROPOSAL: frozenset({S.EVALUATING_POLICY, S.FAILED}),
    S.EVALUATING_POLICY: frozenset({S.AWAITING_APPROVAL, S.READY_TO_EXECUTE, S.REJECTED, S.FAILED}),
    S.AWAITING_APPROVAL: frozenset({S.EVALUATING_POLICY, S.REJECTED, S.CANCELLED, S.FAILED}),
    S.READY_TO_EXECUTE: frozenset({S.EXECUTING, S.REJECTED, S.FAILED, S.CANCELLED}),
    S.EXECUTING: frozenset({S.COMPLETED, S.FAILED}),
    S.COMPLETED: frozenset(),
    S.REJECTED: frozenset(),
    S.CANCELLED: frozenset(),
    S.FAILED: frozenset(),
}

TERMINAL_STATES = frozenset(state for state, nxt in ALLOWED_TRANSITIONS.items() if not nxt)


class InvalidTransition(Exception):
    """Raised when a transition is not allowed by the state machine."""


def validate_transition(current: RunState, new: RunState) -> None:
    if new not in ALLOWED_TRANSITIONS[current]:
        raise InvalidTransition(f"Transition {current.value} -> {new.value} is not allowed.")
