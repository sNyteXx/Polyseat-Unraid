"""Pure lifecycle transition model, without any host operations or file I/O.

Models invariants for later review. It cannot install, start or stop anything.
"""

from __future__ import annotations

from dataclasses import dataclass, replace


class TransitionDenied(ValueError):
    """Unknown states and invalid transitions fail closed."""


@dataclass(frozen=True)
class State:
    phase: str = "ABSENT"
    active: str | None = None
    previous: str | None = None
    staged: str | None = None
    # An external-data fingerprint models a never-owned user asset.
    external_fingerprint: str = "unchanged"


def _digest(value: str | None) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        ch in "0123456789abcdef" for ch in value
    )


def transition(
    state: State,
    action: str,
    *,
    bundle: str | None = None,
    approved: bool = False,
    offline_plan: dict | None = None,
) -> State:
    if not isinstance(state, State) or state.phase not in {
        "ABSENT", "STAGED", "ACTIVE", "RUNNING", "ROLLBACK_PENDING"
    }:
        raise TransitionDenied("unknown or corrupted lifecycle state")
    if state.phase == "ABSENT" and any((state.active, state.previous, state.staged)):
        raise TransitionDenied("inconsistent absent state")
    if state.phase == "STAGED" and not _digest(state.staged):
        raise TransitionDenied("inconsistent staged state")
    if state.phase in ("ACTIVE", "RUNNING") and not _digest(state.active):
        raise TransitionDenied("inconsistent active state")
    if state.phase == "ROLLBACK_PENDING" and not _digest(state.active):
        raise TransitionDenied("inconsistent rollback state")

    if action == "stage":
        if state.phase not in ("ABSENT", "ACTIVE") or not _digest(bundle):
            raise TransitionDenied("stage requires an immutable SHA-256 and stable state")
        if bundle == state.active:
            return state  # Repeated stage is an idempotent no-op.
        return replace(state, phase="STAGED", staged=bundle)

    if action == "activate":
        if not approved:
            raise TransitionDenied("operator approval required for activation")
        if state.phase != "STAGED" or not _digest(state.staged):
            raise TransitionDenied("nothing safely staged")
        return replace(state, phase="ACTIVE", active=state.staged,
                       previous=state.active, staged=None)

    if action == "start":
        if not approved or state.phase not in ("ACTIVE", "RUNNING"):
            raise TransitionDenied("simulation start requires approval and active bundle")
        if (not isinstance(offline_plan, dict)
                or offline_plan.get("classification") != "OFFLINE-PROPOSAL-ONLY"
                or offline_plan.get("installable") is not False
                or offline_plan.get("host_commands") != []):
            raise TransitionDenied("safe offline-only planning evidence required")
        return replace(state, phase="RUNNING")  # Never launches a process.

    if action == "stop":
        if not approved or state.phase not in ("ACTIVE", "RUNNING"):
            raise TransitionDenied("simulation stop requires approval")
        return replace(state, phase="ACTIVE")  # Never signals a process.

    if action == "mark_failed":
        if state.phase not in ("ACTIVE", "RUNNING"):
            raise TransitionDenied("only active/running state can fail activation")
        return replace(state, phase="ROLLBACK_PENDING")

    if action == "rollback":
        if not approved or state.phase != "ROLLBACK_PENDING":
            raise TransitionDenied("rollback requires pending failure and approval")
        if state.previous is None:
            return State(external_fingerprint=state.external_fingerprint)
        if not _digest(state.previous):
            raise TransitionDenied("previous bundle not trustworthy")
        return replace(state, phase="ACTIVE", active=state.previous,
                       previous=None, staged=None)

    if action == "uninstall":
        if not approved or state.phase not in ("ABSENT", "ACTIVE"):
            raise TransitionDenied("uninstall requires stable state and approval")
        return State(external_fingerprint=state.external_fingerprint)

    raise TransitionDenied(f"unsupported action: {action}")
