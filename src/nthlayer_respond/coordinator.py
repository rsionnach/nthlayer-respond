# src/nthlayer_respond/coordinator.py
"""Coordinator state machine — pure transport, no judgment.

Sequences the agent pipeline, persists context after each step,
handles crash recovery via last_completed_step_index, and gates
on escalation / human approval.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import structlog

from nthlayer_respond.types import (
    AgentRole,
    IncidentContext,
    IncidentState,
    TERMINAL_STATES,
)

logger = structlog.get_logger(__name__)

# Step 0: triage (serial)
# Step 1: investigation + communication (parallel)
# Step 2: remediation (serial)
# Step 3: communication resolution update (serial)
PIPELINE: list[list[AgentRole]] = [
    [AgentRole.TRIAGE],
    [AgentRole.INVESTIGATION, AgentRole.COMMUNICATION],
    [AgentRole.REMEDIATION],
    [AgentRole.COMMUNICATION],
]

# Map from pipeline step index to the state the coordinator should set
# before running that step.
_STEP_STATES: dict[int, IncidentState] = {
    0: IncidentState.TRIAGING,
    1: IncidentState.INVESTIGATING,
    2: IncidentState.REMEDIATING,
    3: IncidentState.REMEDIATING,  # still remediating phase for resolution update
}


class Coordinator:
    """Deterministic state machine that sequences agent execution.

    Not an agent — has no model access.  Pure transport: receives context,
    runs the pipeline, persists state, checks gates.
    """

    def __init__(
        self,
        agents: dict[AgentRole, Any],
        context_store: Any,
        verdict_store: Any,
        config: Any,
    ) -> None:
        self._agents = agents
        self._context_store = context_store
        self._verdict_store = verdict_store
        self._config = config

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    async def run(self, context: IncidentContext) -> IncidentContext:
        """Execute the agent pipeline from the current step to completion.

        On success: state -> RESOLVED.
        On escalation gate: state -> ESCALATED.
        On human approval gate: state -> AWAITING_APPROVAL.
        On unrecoverable error: state -> FAILED.
        """
        try:
            return await self._run_pipeline(context)
        except Exception as exc:  # noqa: BLE001
            logger.error("coordinator_unrecoverable", error=str(exc))
            context.state = IncidentState.FAILED
            context.error = str(exc)
            self._context_store.save(context)
            return context

    async def resume(self, incident_id: str) -> IncidentContext:
        """Load a persisted context and continue the pipeline."""
        context = self._context_store.load(incident_id)
        if context is None:
            raise ValueError(f"Incident {incident_id!r} not found in context store")
        return await self.run(context)

    async def approve(self, incident_id: str, approved_by: str | None = None) -> IncidentContext:
        """Execute the approved safe action for a paused incident.

        Requires state == AWAITING_APPROVAL.
        On success: state -> RESOLVED.
        On failure: state -> ESCALATED.

        Args:
            incident_id: Incident to approve.
            approved_by: Identity of the approver (e.g. email). Stored in
                verdict metadata and reasoning for auditability. Defaults to
                "human" when not provided.
        """
        context = self._context_store.load(incident_id)
        if context is None:
            raise ValueError(f"Incident {incident_id!r} not found in context store")
        if context.state != IncidentState.AWAITING_APPROVAL:
            raise ValueError(
                f"Incident {incident_id!r} is in state {context.state.value}, "
                f"not AWAITING_APPROVAL"
            )

        remediation = context.remediation
        if remediation is None:
            raise ValueError(
                f"Incident {incident_id!r} has no remediation result to approve"
            )
        action = remediation.proposed_action
        target = remediation.target

        # Access the registry from the remediation agent
        registry = self._agents[AgentRole.REMEDIATION]._registry

        who = approved_by or "human"
        from nthlayer_learn import create as verdict_create

        try:
            exec_result = await registry.execute(action, target, context)
            remediation.executed = True
            remediation.execution_result = exec_result.get("detail", "")

            v = verdict_create(
                subject={
                    "type": "remediation",
                    "ref": context.id,
                    "summary": f"approved: {action} on {target}",
                },
                judgment={
                    "action": "approve",
                    "confidence": 1.0,
                    "reasoning": f"{who} approved {action} on {target}",
                },
                producer={"system": "nthlayer-respond", "instance": "coordinator"},
                metadata={"custom": {"approved_by": approved_by}} if approved_by else None,
            )
            self._verdict_store.put(v)
            context.verdict_chain.append(v.id)

            context.state = IncidentState.RESOLVED
            self._context_store.save(context)
            return context

        except Exception as exc:  # noqa: BLE001
            logger.error("approve_execution_failed", error=str(exc))

            v = verdict_create(
                subject={
                    "type": "remediation",
                    "ref": context.id,
                    "summary": f"approval failed: {action} on {target}",
                },
                judgment={
                    "action": "escalate",
                    "confidence": 0.0,
                    "reasoning": f"Approved action failed: {exc}",
                },
                producer={"system": "nthlayer-respond", "instance": "coordinator"},
                metadata={"custom": {"approved_by": approved_by}} if approved_by else None,
            )
            self._verdict_store.put(v)
            context.verdict_chain.append(v.id)

            context.state = IncidentState.ESCALATED
            self._context_store.save(context)
            return context

    async def reject(
        self, incident_id: str, reason: str, rejected_by: str | None = None
    ) -> IncidentContext:
        """Reject a proposed remediation action.

        Requires state == AWAITING_APPROVAL.
        Resolves the last remediation verdict as "overridden" and sets
        state -> ESCALATED.

        Args:
            incident_id: Incident to reject.
            reason: Human-readable reason for rejection.
            rejected_by: Identity of the rejector (e.g. email). Stored in
                the override reasoning for auditability. Defaults to "human"
                when not provided.
        """
        context = self._context_store.load(incident_id)
        if context is None:
            raise ValueError(f"Incident {incident_id!r} not found in context store")
        if context.state != IncidentState.AWAITING_APPROVAL:
            raise ValueError(
                f"Incident {incident_id!r} is in state {context.state.value}, "
                f"not AWAITING_APPROVAL"
            )

        remediation = context.remediation
        proposed_action = remediation.proposed_action if remediation else "unknown"
        target = remediation.target if remediation else "unknown"

        who = rejected_by or "human"

        # Resolve the last verdict in the chain as overridden
        if context.verdict_chain:
            last_verdict_id = context.verdict_chain[-1]
            try:
                self._verdict_store.resolve(
                    last_verdict_id,
                    "overridden",
                    override={
                        "by": who,
                        "reasoning": (
                            f"{who} rejected {proposed_action} of {target}: {reason}"
                        ),
                    },
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "reject_verdict_resolve_failed",
                    verdict_id=last_verdict_id,
                    error=str(exc),
                )

        context.state = IncidentState.ESCALATED
        self._context_store.save(context)
        return context

    # ------------------------------------------------------------------ #
    # Internal pipeline execution                                          #
    # ------------------------------------------------------------------ #

    async def _run_pipeline(self, context: IncidentContext) -> IncidentContext:
        """Walk through pipeline steps, running agents and checking gates."""
        start_step = self._next_step(context)
        if start_step is None:
            # Already complete
            if context.state not in TERMINAL_STATES:
                context.state = IncidentState.RESOLVED
                self._context_store.save(context)
            return context

        for step_index in range(start_step, len(PIPELINE)):
            step_roles = PIPELINE[step_index]

            # Before step 3 (second communication): skip if escalated or failed
            if step_index == 3 and context.state in {
                IncidentState.ESCALATED,
                IncidentState.FAILED,
            }:
                break

            # Update state to reflect current phase
            new_state = _STEP_STATES.get(step_index)
            if new_state is not None:
                context.state = new_state

            # Execute step
            if len(step_roles) == 1:
                await self._run_serial_step(context, step_roles[0])
            else:
                await self._run_parallel_step(context, step_roles)

            # Persist after step
            context.last_completed_step_index = step_index
            self._context_store.save(context)

            # Gate: escalation check
            if self._check_escalation(context):
                context.state = IncidentState.ESCALATED
                self._context_store.save(context)
                return context

            # Gate: human approval (after remediation step, index 2)
            if step_index == 2:
                if (
                    context.remediation is not None
                    and context.remediation.requires_human_approval
                ):
                    context.state = IncidentState.AWAITING_APPROVAL
                    context.updated_at = datetime.now(timezone.utc).isoformat()
                    self._context_store.save(context)
                    return context

        # All steps complete
        if context.state not in TERMINAL_STATES:
            context.state = IncidentState.RESOLVED
            self._context_store.save(context)

        return context

    async def _run_serial_step(
        self, context: IncidentContext, role: AgentRole
    ) -> None:
        """Run a single agent synchronously."""
        agent = self._agents[role]
        logger.info("step_start", role=role.value, incident=context.id)
        await agent.execute(context)
        logger.info("step_complete", role=role.value, incident=context.id)

    async def _run_parallel_step(
        self, context: IncidentContext, roles: list[AgentRole]
    ) -> None:
        """Run multiple agents in parallel via asyncio.gather.

        Each agent writes to a different field on context, so no data race.
        Investigation failure is critical; communication failure is non-blocking.
        """
        tasks = [self._agents[role].execute(context) for role in roles]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for role, result in zip(roles, results):
            if isinstance(result, Exception):
                if role == AgentRole.INVESTIGATION:
                    logger.error(
                        "investigation_failed",
                        error=str(result),
                        incident=context.id,
                    )
                else:
                    logger.warning(
                        "communication_failed",
                        error=str(result),
                        incident=context.id,
                    )

    # ------------------------------------------------------------------ #
    # Gates                                                                #
    # ------------------------------------------------------------------ #

    def _check_escalation(self, context: IncidentContext) -> bool:
        """Return True if any verdict in the chain has action=escalate
        with confidence below the configured threshold."""
        threshold = self._config.escalation_threshold
        for verdict_id in context.verdict_chain:
            try:
                verdict = self._verdict_store.get(verdict_id)
            except Exception:  # noqa: BLE001
                continue
            if verdict is None:
                continue
            if (
                verdict.judgment.action == "escalate"
                and verdict.judgment.confidence < threshold
            ):
                return True
        return False

    @staticmethod
    def _next_step(context: IncidentContext) -> int | None:
        """Determine the next pipeline step to execute.

        Returns None if all steps are complete.
        """
        if context.last_completed_step_index is None:
            return 0
        next_idx = context.last_completed_step_index + 1
        if next_idx >= len(PIPELINE):
            return None
        return next_idx
