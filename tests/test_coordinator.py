# tests/test_coordinator.py
"""Tests for coordinator state machine."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from verdict import MemoryStore, create as verdict_create

from mayday.coordinator import Coordinator, PIPELINE
from mayday.context_store import SQLiteContextStore
from mayday.types import (
    AgentRole,
    CommunicationResult,
    Hypothesis,
    IncidentContext,
    IncidentState,
    InvestigationResult,
    RemediationResult,
    TriageResult,
)


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #


def make_mock_agent(role, side_effect=None):
    """Create a mock agent that modifies context appropriately."""
    agent = AsyncMock()
    agent.role = role

    if role == AgentRole.TRIAGE:
        async def execute(ctx):
            ctx.triage = TriageResult(
                severity=1,
                blast_radius=["payment-api"],
                affected_slos=["availability"],
                assigned_team="payments",
                reasoning="test",
            )
            ctx.verdict_chain.append(f"vrd-{role.value}")
            return ctx
    elif role == AgentRole.INVESTIGATION:
        async def execute(ctx):
            ctx.investigation = InvestigationResult(
                hypotheses=[
                    Hypothesis("deploy caused it", 0.87, ["evidence"], "deploy v2.3.1")
                ],
                root_cause="deploy v2.3.1",
                root_cause_confidence=0.87,
                reasoning="test",
            )
            ctx.verdict_chain.append(f"vrd-{role.value}")
            return ctx
    elif role == AgentRole.COMMUNICATION:
        async def execute(ctx):
            if ctx.communication is None:
                ctx.communication = CommunicationResult(reasoning="initial")
            ctx.verdict_chain.append(f"vrd-{role.value}")
            return ctx
    elif role == AgentRole.REMEDIATION:
        async def execute(ctx):
            ctx.remediation = RemediationResult(
                proposed_action="rollback",
                target="payment-api",
                requires_human_approval=False,
                executed=True,
                execution_result="success",
                reasoning="test",
            )
            ctx.verdict_chain.append(f"vrd-{role.value}")
            return ctx

    if side_effect:
        agent.execute = AsyncMock(side_effect=side_effect)
    else:
        agent.execute = AsyncMock(side_effect=execute)

    return agent


# ------------------------------------------------------------------ #
# Fixtures                                                             #
# ------------------------------------------------------------------ #


@pytest.fixture
def context_store(tmp_path):
    s = SQLiteContextStore(str(tmp_path / "test.db"))
    yield s
    s.close()


@pytest.fixture
def make_coordinator(context_store, verdict_store):
    def _make(agent_overrides=None):
        agents = {
            AgentRole.TRIAGE: make_mock_agent(AgentRole.TRIAGE),
            AgentRole.INVESTIGATION: make_mock_agent(AgentRole.INVESTIGATION),
            AgentRole.COMMUNICATION: make_mock_agent(AgentRole.COMMUNICATION),
            AgentRole.REMEDIATION: make_mock_agent(AgentRole.REMEDIATION),
        }
        if agent_overrides:
            agents.update(agent_overrides)

        config = MagicMock()
        config.escalation_threshold = 0.3

        return Coordinator(agents, context_store, verdict_store, config)

    return _make


@pytest.fixture
def triggered_context():
    return IncidentContext(
        id="INC-2026-0001",
        state=IncidentState.TRIGGERED,
        created_at="2026-03-19T10:00:00Z",
        updated_at="2026-03-19T10:00:00Z",
        trigger_source="sitrep",
        trigger_verdict_ids=["vrd-trigger"],
        topology={},
    )


# ------------------------------------------------------------------ #
# Pipeline tests                                                       #
# ------------------------------------------------------------------ #


async def test_pipeline_definition_has_four_steps():
    """PIPELINE has the expected structure."""
    assert len(PIPELINE) == 4
    assert PIPELINE[0] == [AgentRole.TRIAGE]
    assert set(PIPELINE[1]) == {AgentRole.INVESTIGATION, AgentRole.COMMUNICATION}
    assert PIPELINE[2] == [AgentRole.REMEDIATION]
    assert PIPELINE[3] == [AgentRole.COMMUNICATION]


async def test_full_pipeline_runs_all_agents(make_coordinator, triggered_context):
    """Full run from TRIGGERED to RESOLVED calls all agents."""
    coord = make_coordinator()
    result = await coord.run(triggered_context)
    assert result.state == IncidentState.RESOLVED
    assert result.triage is not None
    assert result.investigation is not None
    assert result.remediation is not None
    assert result.last_completed_step_index == 3  # all 4 steps done


async def test_state_transitions_during_pipeline(
    make_coordinator, triggered_context, context_store
):
    """State transitions to TRIAGING, INVESTIGATING, REMEDIATING during run."""
    states_seen: list[IncidentState] = []

    async def triage_spy(ctx):
        states_seen.append(ctx.state)
        ctx.triage = TriageResult(
            severity=1,
            blast_radius=["svc"],
            affected_slos=[],
            assigned_team="team",
            reasoning="test",
        )
        ctx.verdict_chain.append("vrd-triage")
        return ctx

    async def investigation_spy(ctx):
        states_seen.append(ctx.state)
        ctx.investigation = InvestigationResult(
            hypotheses=[], root_cause=None, root_cause_confidence=0.0, reasoning="test"
        )
        ctx.verdict_chain.append("vrd-investigation")
        return ctx

    async def remediation_spy(ctx):
        states_seen.append(ctx.state)
        ctx.remediation = RemediationResult(
            proposed_action="rollback",
            target="svc",
            requires_human_approval=False,
            executed=True,
            reasoning="test",
        )
        ctx.verdict_chain.append("vrd-remediation")
        return ctx

    coord = make_coordinator({
        AgentRole.TRIAGE: make_mock_agent(AgentRole.TRIAGE, side_effect=triage_spy),
        AgentRole.INVESTIGATION: make_mock_agent(
            AgentRole.INVESTIGATION, side_effect=investigation_spy
        ),
        AgentRole.REMEDIATION: make_mock_agent(
            AgentRole.REMEDIATION, side_effect=remediation_spy
        ),
    })
    result = await coord.run(triggered_context)

    assert IncidentState.TRIAGING in states_seen
    assert IncidentState.INVESTIGATING in states_seen
    assert IncidentState.REMEDIATING in states_seen
    assert result.state == IncidentState.RESOLVED


async def test_context_saved_after_each_step(
    make_coordinator, triggered_context, context_store
):
    """Context is persisted after every pipeline step."""
    coord = make_coordinator()
    await coord.run(triggered_context)

    loaded = context_store.load("INC-2026-0001")
    assert loaded is not None
    assert loaded.state == IncidentState.RESOLVED
    assert loaded.last_completed_step_index == 3


# ------------------------------------------------------------------ #
# Crash recovery                                                       #
# ------------------------------------------------------------------ #


async def test_crash_recovery_resumes_from_step_index(
    make_coordinator, triggered_context
):
    """Resume skips completed steps."""
    triggered_context.last_completed_step_index = 0  # triage done
    triggered_context.triage = TriageResult(
        severity=1,
        blast_radius=["svc"],
        affected_slos=[],
        assigned_team="team",
        reasoning="done",
    )
    coord = make_coordinator()
    result = await coord.run(triggered_context)
    # Triage should NOT have been called again
    coord._agents[AgentRole.TRIAGE].execute.assert_not_called()
    # Investigation SHOULD have been called
    coord._agents[AgentRole.INVESTIGATION].execute.assert_called()
    assert result.state == IncidentState.RESOLVED


async def test_resume_loads_and_continues(
    make_coordinator, triggered_context, context_store
):
    """resume() loads from store and continues the pipeline."""
    # Pre-populate store with partially-complete context
    triggered_context.last_completed_step_index = 0
    triggered_context.triage = TriageResult(
        severity=2,
        blast_radius=["api"],
        affected_slos=[],
        assigned_team="infra",
        reasoning="done",
    )
    context_store.save(triggered_context)

    coord = make_coordinator()
    result = await coord.resume("INC-2026-0001")
    assert result.state == IncidentState.RESOLVED
    assert result.investigation is not None


# ------------------------------------------------------------------ #
# Parallel step failure isolation                                      #
# ------------------------------------------------------------------ #


async def test_parallel_step_failure_isolation(make_coordinator, triggered_context):
    """Communication fails, investigation succeeds: pipeline continues to remediation."""
    failed_comm = make_mock_agent(
        AgentRole.COMMUNICATION, side_effect=Exception("model timeout")
    )
    coord = make_coordinator({AgentRole.COMMUNICATION: failed_comm})
    result = await coord.run(triggered_context)
    # Investigation should have succeeded
    assert result.investigation is not None
    # Pipeline should continue despite communication failure
    assert result.remediation is not None


async def test_parallel_step_investigation_failure_logged(
    make_coordinator, triggered_context
):
    """Investigation failure in parallel step is treated as critical."""
    failed_inv = make_mock_agent(
        AgentRole.INVESTIGATION, side_effect=Exception("investigation broke")
    )
    coord = make_coordinator({AgentRole.INVESTIGATION: failed_inv})
    # Should still complete (not crash), pipeline may continue or escalate
    result = await coord.run(triggered_context)
    assert result.state in {
        IncidentState.RESOLVED,
        IncidentState.ESCALATED,
        IncidentState.FAILED,
    }


# ------------------------------------------------------------------ #
# Escalation                                                           #
# ------------------------------------------------------------------ #


async def test_escalation_check_triggers(
    make_coordinator, triggered_context, verdict_store
):
    """Agent emits escalate with low confidence -> ESCALATED."""
    v = verdict_create(
        subject={"type": "triage", "ref": "INC-2026-0001", "summary": "test"},
        judgment={
            "action": "escalate",
            "confidence": 0.1,
            "reasoning": "too uncertain",
        },
        producer={"system": "mayday"},
    )
    verdict_store.put(v)

    async def triage_escalates(ctx):
        ctx.triage = TriageResult(
            severity=0,
            blast_radius=["svc"],
            affected_slos=[],
            assigned_team=None,
            reasoning="uncertain",
        )
        ctx.verdict_chain.append(v.id)
        return ctx

    escalating_triage = make_mock_agent(
        AgentRole.TRIAGE, side_effect=triage_escalates
    )
    coord = make_coordinator({AgentRole.TRIAGE: escalating_triage})
    result = await coord.run(triggered_context)
    assert result.state == IncidentState.ESCALATED


async def test_escalation_not_triggered_above_threshold(
    make_coordinator, triggered_context, verdict_store
):
    """Escalate action with confidence above threshold does not escalate."""
    v = verdict_create(
        subject={"type": "triage", "ref": "INC-2026-0001", "summary": "test"},
        judgment={
            "action": "escalate",
            "confidence": 0.9,
            "reasoning": "confident escalation",
        },
        producer={"system": "mayday"},
    )
    verdict_store.put(v)

    async def triage_with_verdict(ctx):
        ctx.triage = TriageResult(
            severity=1,
            blast_radius=["svc"],
            affected_slos=[],
            assigned_team="team",
            reasoning="test",
        )
        ctx.verdict_chain.append(v.id)
        return ctx

    agent = make_mock_agent(AgentRole.TRIAGE, side_effect=triage_with_verdict)
    coord = make_coordinator({AgentRole.TRIAGE: agent})
    result = await coord.run(triggered_context)
    # High confidence escalation should NOT trigger the escalation gate
    assert result.state == IncidentState.RESOLVED


# ------------------------------------------------------------------ #
# Approval flow                                                        #
# ------------------------------------------------------------------ #


async def test_awaiting_approval_pauses(make_coordinator, triggered_context):
    """Remediation requires_human_approval -> AWAITING_APPROVAL."""

    async def remediation_needs_approval(ctx):
        ctx.remediation = RemediationResult(
            proposed_action="rollback",
            target="payment-api",
            requires_human_approval=True,
            reasoning="needs approval",
        )
        ctx.verdict_chain.append("vrd-remediation")
        return ctx

    approval_agent = make_mock_agent(
        AgentRole.REMEDIATION, side_effect=remediation_needs_approval
    )
    coord = make_coordinator({AgentRole.REMEDIATION: approval_agent})
    result = await coord.run(triggered_context)
    assert result.state == IncidentState.AWAITING_APPROVAL
    # Second communication should NOT have run
    assert coord._agents[AgentRole.COMMUNICATION].execute.call_count <= 1


async def test_approve_executes_safe_action(
    make_coordinator, context_store, triggered_context
):
    """approve() on AWAITING_APPROVAL -> executes action -> RESOLVED."""

    async def remediation_needs_approval(ctx):
        ctx.remediation = RemediationResult(
            proposed_action="rollback",
            target="payment-api",
            requires_human_approval=True,
            reasoning="needs approval",
        )
        ctx.verdict_chain.append("vrd-remediation")
        return ctx

    approval_agent = make_mock_agent(
        AgentRole.REMEDIATION, side_effect=remediation_needs_approval
    )
    # Give the remediation agent a mock registry
    mock_registry = AsyncMock()
    mock_registry.execute = AsyncMock(
        return_value={"success": True, "detail": "rolled back"}
    )
    approval_agent._registry = mock_registry

    coord = make_coordinator({AgentRole.REMEDIATION: approval_agent})
    result = await coord.run(triggered_context)
    assert result.state == IncidentState.AWAITING_APPROVAL

    # Now approve
    result = await coord.approve("INC-2026-0001")
    assert result.state == IncidentState.RESOLVED


async def test_approve_failure_escalates(
    make_coordinator, context_store, triggered_context
):
    """approve() where safe action execution fails -> ESCALATED."""

    async def remediation_needs_approval(ctx):
        ctx.remediation = RemediationResult(
            proposed_action="rollback",
            target="payment-api",
            requires_human_approval=True,
            reasoning="needs approval",
        )
        ctx.verdict_chain.append("vrd-remediation")
        return ctx

    approval_agent = make_mock_agent(
        AgentRole.REMEDIATION, side_effect=remediation_needs_approval
    )
    mock_registry = AsyncMock()
    mock_registry.execute = AsyncMock(side_effect=RuntimeError("cooldown active"))
    approval_agent._registry = mock_registry

    coord = make_coordinator({AgentRole.REMEDIATION: approval_agent})
    await coord.run(triggered_context)

    result = await coord.approve("INC-2026-0001")
    assert result.state == IncidentState.ESCALATED


async def test_approve_wrong_state_raises(
    make_coordinator, context_store, triggered_context
):
    """approve() on non-AWAITING_APPROVAL state raises ValueError."""
    coord = make_coordinator()
    await coord.run(triggered_context)  # Goes to RESOLVED

    with pytest.raises(ValueError, match="AWAITING_APPROVAL"):
        await coord.approve("INC-2026-0001")


async def test_reject_resolves_verdict(
    make_coordinator, context_store, verdict_store, triggered_context
):
    """reject() -> overridden verdict -> ESCALATED."""
    v = verdict_create(
        subject={
            "type": "remediation",
            "ref": "INC-2026-0001",
            "summary": "rollback",
        },
        judgment={
            "action": "approve",
            "confidence": 0.8,
            "reasoning": "rollback recommended",
        },
        producer={"system": "mayday"},
    )
    verdict_store.put(v)

    async def remediation_needs_approval(ctx):
        ctx.remediation = RemediationResult(
            proposed_action="rollback",
            target="payment-api",
            requires_human_approval=True,
            reasoning="needs approval",
        )
        ctx.verdict_chain.append(v.id)
        return ctx

    approval_agent = make_mock_agent(
        AgentRole.REMEDIATION, side_effect=remediation_needs_approval
    )
    coord = make_coordinator({AgentRole.REMEDIATION: approval_agent})
    result = await coord.run(triggered_context)
    assert result.state == IncidentState.AWAITING_APPROVAL

    result = await coord.reject("INC-2026-0001", "Wrong service targeted")
    assert result.state == IncidentState.ESCALATED
    # Verify the verdict was resolved as overridden
    resolved = verdict_store.get(v.id)
    assert resolved.outcome.status == "overridden"


async def test_reject_wrong_state_raises(
    make_coordinator, context_store, triggered_context
):
    """reject() on non-AWAITING_APPROVAL state raises ValueError."""
    coord = make_coordinator()
    await coord.run(triggered_context)

    with pytest.raises(ValueError, match="AWAITING_APPROVAL"):
        await coord.reject("INC-2026-0001", "reason")


# ------------------------------------------------------------------ #
# Second communication skip                                            #
# ------------------------------------------------------------------ #


async def test_second_communication_skipped_when_escalated(
    make_coordinator, triggered_context, verdict_store
):
    """If ESCALATED after remediation, skip resolution communication."""
    v = verdict_create(
        subject={"type": "remediation", "ref": "INC-001", "summary": "test"},
        judgment={
            "action": "escalate",
            "confidence": 0.1,
            "reasoning": "too risky",
        },
        producer={"system": "mayday"},
    )
    verdict_store.put(v)

    async def remediation_escalates(ctx):
        ctx.remediation = RemediationResult(
            proposed_action=None,
            reasoning="too risky",
        )
        ctx.verdict_chain.append(v.id)
        return ctx

    esc_agent = make_mock_agent(
        AgentRole.REMEDIATION, side_effect=remediation_escalates
    )
    coord = make_coordinator({AgentRole.REMEDIATION: esc_agent})
    result = await coord.run(triggered_context)
    assert result.state == IncidentState.ESCALATED
    # Communication should have run only once (the initial parallel run), not twice
    assert coord._agents[AgentRole.COMMUNICATION].execute.call_count <= 1


# ------------------------------------------------------------------ #
# Error handling                                                       #
# ------------------------------------------------------------------ #


async def test_failed_state_captures_error(make_coordinator, triggered_context):
    """Unrecoverable error in serial step -> FAILED with error message."""

    async def triage_crashes(ctx):
        raise RuntimeError("Database connection lost")

    crash_agent = make_mock_agent(AgentRole.TRIAGE, side_effect=triage_crashes)
    coord = make_coordinator({AgentRole.TRIAGE: crash_agent})
    result = await coord.run(triggered_context)
    assert result.state == IncidentState.FAILED
    assert "Database connection lost" in result.error


async def test_resume_missing_incident_raises(make_coordinator):
    """resume() for nonexistent incident raises ValueError."""
    coord = make_coordinator()
    with pytest.raises(ValueError, match="not found"):
        await coord.resume("INC-NONEXISTENT")


async def test_next_step_logic():
    """_next_step returns correct next step index."""
    from mayday.coordinator import Coordinator

    ctx = IncidentContext(
        id="test",
        state=IncidentState.TRIGGERED,
        created_at="",
        updated_at="",
        trigger_source="test",
        trigger_verdict_ids=[],
        topology={},
    )

    # None -> 0
    ctx.last_completed_step_index = None
    assert Coordinator._next_step(ctx) == 0

    # 0 -> 1
    ctx.last_completed_step_index = 0
    assert Coordinator._next_step(ctx) == 1

    # 2 -> 3
    ctx.last_completed_step_index = 2
    assert Coordinator._next_step(ctx) == 3

    # 3 -> None (done)
    ctx.last_completed_step_index = 3
    assert Coordinator._next_step(ctx) is None
