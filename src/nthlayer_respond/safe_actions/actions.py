# src/nthlayer_respond/safe_actions/actions.py
"""Built-in safe actions — policy from YAML, handlers in Python.

The registry YAML (registry/safe-actions.yaml) defines which actions exist,
their descriptions, risk levels, approval requirements, and applicability.
This file provides the handler stubs and loads the YAML policy.
"""
from __future__ import annotations

from pathlib import Path

import structlog
import yaml

from nthlayer_respond.safe_actions.registry import SafeAction, SafeActionRegistry
from nthlayer_respond.types import IncidentContext

logger = structlog.get_logger(__name__)

_REGISTRY_PATH = Path(__file__).parent.parent.parent.parent / "registry" / "safe-actions.yaml"


# ------------------------------------------------------------------ #
# Handler stubs (real integrations wired in later phases)              #
# ------------------------------------------------------------------ #

async def _rollback_handler(target: str, context: IncidentContext, **kwargs) -> dict:
    logger.info("[STUB] rollback: would roll back service", target=target, incident_id=context.id)
    return {"success": True, "detail": f"Rollback of {target!r} initiated (stub)."}


async def _scale_up_handler(target: str, context: IncidentContext, **kwargs) -> dict:
    logger.info("[STUB] scale_up: would scale up service", target=target, incident_id=context.id)
    return {"success": True, "detail": f"Scale-up of {target!r} initiated (stub)."}


async def _disable_feature_flag_handler(target: str, context: IncidentContext, **kwargs) -> dict:
    logger.info("[STUB] disable_feature_flag: would disable flag", target=target, incident_id=context.id)
    return {"success": True, "detail": f"Feature flag {target!r} disabled (stub)."}


async def _reduce_autonomy_handler(target: str, context: IncidentContext, **kwargs) -> dict:
    logger.info("[STUB] reduce_autonomy: would reduce autonomy", target=target, incident_id=context.id)
    return {"success": True, "detail": f"Autonomy for {target!r} reduced (stub)."}


async def _pause_pipeline_handler(target: str, context: IncidentContext, **kwargs) -> dict:
    logger.info("[STUB] pause_pipeline: would pause pipeline", target=target, incident_id=context.id)
    return {"success": True, "detail": f"Pipeline {target!r} paused (stub)."}


# Handler lookup — maps action name to its Python handler
_HANDLERS = {
    "rollback": _rollback_handler,
    "scale_up": _scale_up_handler,
    "disable_feature_flag": _disable_feature_flag_handler,
    "reduce_autonomy": _reduce_autonomy_handler,
    "pause_pipeline": _pause_pipeline_handler,
}


# ------------------------------------------------------------------ #
# Registration from YAML                                               #
# ------------------------------------------------------------------ #

def register_builtin_actions(registry: SafeActionRegistry) -> None:
    """Load safe action policy from YAML and register with handlers."""
    policy = load_safe_action_policy()

    for name, spec in policy.items():
        handler = _HANDLERS.get(name)
        if handler is None:
            logger.warning("Safe action %r in YAML has no handler — skipping", name)
            continue

        registry.register(SafeAction(
            name=name,
            description=spec.get("description", "").strip(),
            target_type=spec.get("target_type", "service"),
            requires_approval=spec.get("requires_approval", True),
            cooldown_seconds=spec.get("cooldown_seconds", 300),
            handler=handler,
        ))


def load_safe_action_policy(path: Path | None = None) -> dict:
    """Load the safe action policy YAML. Returns {name: spec_dict}."""
    p = path or _REGISTRY_PATH
    with open(p) as f:
        raw = yaml.safe_load(f)
    return raw.get("actions", {})
