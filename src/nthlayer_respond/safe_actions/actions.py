# src/nthlayer_respond/safe_actions/actions.py
"""Built-in safe actions (Tier 1 stubs).

All handlers log intent and return simulated success.  Real integrations
(kubectl, feature-flag API, etc.) are wired in later phases.
"""
from __future__ import annotations

import structlog

from nthlayer_respond.safe_actions.registry import SafeAction, SafeActionRegistry
from nthlayer_respond.types import IncidentContext

logger = structlog.get_logger(__name__)


# ------------------------------------------------------------------ #
# Handler stubs                                                        #
# ------------------------------------------------------------------ #

async def _rollback_handler(target: str, context: IncidentContext, **kwargs) -> dict:
    logger.info("[STUB] rollback: would roll back service", target=target, incident_id=context.id)
    return {"success": True, "detail": f"Rollback of {target!r} initiated (stub)."}


async def _scale_up_handler(target: str, context: IncidentContext, **kwargs) -> dict:
    logger.info("[STUB] scale_up: would scale up service", target=target, incident_id=context.id)
    return {"success": True, "detail": f"Scale-up of {target!r} initiated (stub)."}


async def _disable_feature_flag_handler(target: str, context: IncidentContext, **kwargs) -> dict:
    logger.info(
        "[STUB] disable_feature_flag: would disable flag %r (incident %s)", target, context.id
    )
    return {"success": True, "detail": f"Feature flag {target!r} disabled (stub)."}


async def _reduce_autonomy_handler(target: str, context: IncidentContext, **kwargs) -> dict:
    logger.info(
        "[STUB] reduce_autonomy: would reduce autonomy for arbiter %r (incident %s)",
        target,
        context.id,
    )
    return {"success": True, "detail": f"Autonomy for {target!r} reduced (stub)."}


async def _pause_pipeline_handler(target: str, context: IncidentContext, **kwargs) -> dict:
    logger.info(
        "[STUB] pause_pipeline: would pause pipeline %r (incident %s)", target, context.id
    )
    return {"success": True, "detail": f"Pipeline {target!r} paused (stub)."}


# ------------------------------------------------------------------ #
# Registration                                                         #
# ------------------------------------------------------------------ #

def register_builtin_actions(registry: SafeActionRegistry) -> None:
    """Register all five built-in Tier 1 safe actions into *registry*."""
    registry.register(SafeAction(
        name="rollback",
        description=(
            "Roll back a service to its previous stable deployment. "
            "Use when a recent deployment is the suspected root cause."
        ),
        target_type="service",
        requires_approval=True,
        cooldown_seconds=300,
        handler=_rollback_handler,
    ))

    registry.register(SafeAction(
        name="scale_up",
        description=(
            "Increase the replica count of a service to handle elevated load. "
            "Use when error rate is driven by capacity exhaustion."
        ),
        target_type="service",
        requires_approval=False,
        cooldown_seconds=120,
        handler=_scale_up_handler,
    ))

    registry.register(SafeAction(
        name="disable_feature_flag",
        description=(
            "Disable a feature flag to revert to the previous behaviour. "
            "Use when a recently enabled flag correlates with the incident."
        ),
        target_type="feature_flag",
        requires_approval=True,
        cooldown_seconds=60,
        handler=_disable_feature_flag_handler,
    ))

    registry.register(SafeAction(
        name="reduce_autonomy",
        description=(
            "Reduce the autonomy level of an Arbiter-governed agent. "
            "Autonomy ratchet is one-way safe: can reduce, never increase without approval."
        ),
        target_type="arbiter",
        requires_approval=False,
        cooldown_seconds=0,
        handler=_reduce_autonomy_handler,
    ))

    registry.register(SafeAction(
        name="pause_pipeline",
        description=(
            "Pause a deployment or processing pipeline to stop further propagation. "
            "Use when an in-progress rollout is worsening an incident."
        ),
        target_type="service",
        requires_approval=True,
        cooldown_seconds=60,
        handler=_pause_pipeline_handler,
    ))
