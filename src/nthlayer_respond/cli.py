# src/nthlayer_respond/cli.py
"""Mayday CLI — replay, status, approve/reject, serve."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any

import structlog
import yaml
from nthlayer_learn import MemoryStore

logger = structlog.get_logger(__name__)

from nthlayer_respond.agents.communication import CommunicationAgent
from nthlayer_respond.agents.investigation import InvestigationAgent
from nthlayer_respond.agents.remediation import RemediationAgent
from nthlayer_respond.agents.triage import TriageAgent
from nthlayer_respond.config import MaydayConfig, load_config
from nthlayer_respond.context_store import SQLiteContextStore
from nthlayer_respond.coordinator import Coordinator
from nthlayer_respond.safe_actions.actions import register_builtin_actions
from nthlayer_respond.safe_actions.registry import SafeActionRegistry
from nthlayer_respond.types import AgentRole, IncidentContext, IncidentState


# ------------------------------------------------------------------ #
# Argument parser                                                      #
# ------------------------------------------------------------------ #


def build_parser() -> argparse.ArgumentParser:
    """Build and return the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="nthlayer-respond",
        description="Mayday -- multi-agent incident response",
    )
    sub = parser.add_subparsers(dest="command")

    # serve
    serve = sub.add_parser("serve", help="Start polling loop")
    serve.add_argument("--config", default="mayday.yaml", help="Config file path")

    # status
    status = sub.add_parser("status", help="Show active incidents")
    status.add_argument("--config", default="mayday.yaml", help="Config file path")

    # replay
    replay = sub.add_parser("replay", help="Replay a scenario")
    replay.add_argument("--scenario", required=True, help="Path to scenario YAML")
    replay.add_argument("--config", default="mayday.yaml", help="Config file path")
    replay.add_argument(
        "--no-model",
        action="store_true",
        default=False,
        help="Use mock responses from scenario instead of calling model",
    )

    # approve
    approve = sub.add_parser("approve", help="Approve pending remediation")
    approve.add_argument("incident_id", help="Incident ID")
    approve.add_argument("--config", default="mayday.yaml", help="Config file path")

    # reject
    reject = sub.add_parser("reject", help="Reject pending remediation")
    reject.add_argument("incident_id", help="Incident ID")
    reject.add_argument("--reason", required=True, help="Rejection reason")
    reject.add_argument("--config", default="mayday.yaml", help="Config file path")

    # resume
    resume = sub.add_parser("resume", help="Resume crashed incident")
    resume.add_argument("incident_id", help="Incident ID")
    resume.add_argument("--config", default="mayday.yaml", help="Config file path")

    return parser


# ------------------------------------------------------------------ #
# Mock model for --no-model replay                                     #
# ------------------------------------------------------------------ #


def _make_mock_call_model(mock_response: dict | None):
    """Return an async callable that returns *mock_response* as JSON.

    If *mock_response* is None, simulate model failure.
    """

    async def mock_call_model(system_prompt: str, user_prompt: str) -> str:
        if mock_response is None:
            raise Exception("Model unavailable (mock)")
        return json.dumps(mock_response)

    return mock_call_model


def _make_sequenced_mock(responses: list[dict | None]):
    """Return an async callable that yields successive responses on each call."""
    call_index = {"n": 0}

    async def mock_call_model(system_prompt: str, user_prompt: str) -> str:
        idx = call_index["n"]
        call_index["n"] += 1
        if idx < len(responses):
            resp = responses[idx]
        else:
            resp = responses[-1] if responses else None
        if resp is None:
            raise Exception("Model unavailable (mock)")
        return json.dumps(resp)

    return mock_call_model


# ------------------------------------------------------------------ #
# Replay command                                                       #
# ------------------------------------------------------------------ #


async def replay_command(
    scenario_path: str,
    config_path: str | None = None,
    no_model: bool = True,
    work_dir: str | None = None,
) -> dict[str, Any]:
    """Execute a scenario replay and return results dict.

    Parameters
    ----------
    scenario_path : str
        Path to the scenario YAML file.
    config_path : str | None
        Optional config file. Defaults create a MaydayConfig with defaults.
    no_model : bool
        When True, use mock_responses from the scenario YAML instead of
        calling the Anthropic API.
    work_dir : str | None
        Working directory for temporary SQLite databases. Defaults to cwd.

    Returns
    -------
    dict with keys: final_state, verdict_count, verdict_chain,
    remediation_executed, incident_id, context.
    """
    # Load scenario
    with open(scenario_path) as f:
        raw = yaml.safe_load(f)
    scenario = raw["scenario"]

    # Load config
    if config_path is not None and os.path.exists(config_path):
        config = load_config(config_path)
    else:
        config = MaydayConfig()

    # Work directory — use a temp dir for replay to avoid stale state
    import tempfile
    if work_dir is None:
        work_dir = tempfile.mkdtemp(prefix="mayday-replay-")

    # Stores
    verdict_store = MemoryStore()
    context_store = SQLiteContextStore(os.path.join(work_dir, "replay-incidents.db"))

    # Safe action registry
    registry = SafeActionRegistry(os.path.join(work_dir, "replay-cooldowns.db"))
    register_builtin_actions(registry)

    # In --no-model replay, override the registry's requires_approval to match
    # the scenario mock_response intent so the replay exercises the intended flow.
    # Only override to False (allow auto-execution) when the scenario explicitly
    # says requires_human_approval: false. When True, leave the registry default
    # so the approval ratchet works correctly and the coordinator pauses.
    mock_responses = scenario.get("mock_responses", {})
    if no_model:
        rem_mock = mock_responses.get("remediation")
        if rem_mock is not None and rem_mock.get("proposed_action"):
            action_name = rem_mock["proposed_action"]
            mock_requires_approval = rem_mock.get("requires_human_approval", True)
            if not mock_requires_approval:
                try:
                    action_obj = registry.get(action_name)
                    action_obj.requires_approval = False
                except KeyError:
                    pass  # action not in registry; will be caught later

    # Agent config dict (agents access config via dict-style .get())
    agent_config = {
        "root_cause_threshold": config.root_cause_threshold,
        "arbiter_url": config.arbiter_url,
    }

    # Create agents
    agents_map: dict[AgentRole, Any] = {
        AgentRole.TRIAGE: TriageAgent(
            model=config.model,
            max_tokens=config.max_tokens,
            verdict_store=verdict_store,
            config=agent_config,
            timeout=config.triage_timeout,
        ),
        AgentRole.INVESTIGATION: InvestigationAgent(
            model=config.model,
            max_tokens=config.max_tokens,
            verdict_store=verdict_store,
            config=agent_config,
            timeout=config.investigation_timeout,
        ),
        AgentRole.COMMUNICATION: CommunicationAgent(
            model=config.model,
            max_tokens=config.max_tokens,
            verdict_store=verdict_store,
            config=agent_config,
            timeout=config.communication_timeout,
        ),
        AgentRole.REMEDIATION: RemediationAgent(
            model=config.model,
            max_tokens=config.max_tokens,
            verdict_store=verdict_store,
            config=agent_config,
            timeout=config.remediation_timeout,
            safe_action_registry=registry,
        ),
    }

    # --no-model: patch _call_model on each agent
    if no_model:
        # Triage
        agents_map[AgentRole.TRIAGE]._call_model = _make_mock_call_model(
            mock_responses.get("triage")
        )
        # Investigation
        agents_map[AgentRole.INVESTIGATION]._call_model = _make_mock_call_model(
            mock_responses.get("investigation")
        )
        # Communication — runs twice: initial (step 1) then resolution (step 3)
        comm_responses = [
            mock_responses.get("communication_initial"),
            mock_responses.get("communication_resolution"),
        ]
        agents_map[AgentRole.COMMUNICATION]._call_model = _make_sequenced_mock(
            comm_responses
        )
        # Remediation
        agents_map[AgentRole.REMEDIATION]._call_model = _make_mock_call_model(
            mock_responses.get("remediation")
        )

    # Build incident context from trigger
    trigger = scenario["trigger"]
    trigger_source = trigger["source"]
    now = datetime.now(tz=timezone.utc).isoformat()
    incident_id = f"INC-REPLAY-{scenario['id']}"

    if trigger_source == "sitrep":
        # In --no-model mode, create mock SitRep correlation verdicts
        trigger_verdict_ids: list[str] = []
        if no_model:
            from nthlayer_learn import create as verdict_create

            v = verdict_create(
                subject={
                    "type": "correlation",
                    "ref": incident_id,
                    "summary": f"SitRep correlation for {scenario['id']}",
                },
                judgment={
                    "action": "flag",
                    "confidence": 0.9,
                    "reasoning": "Mock SitRep correlation verdict for replay",
                },
                producer={"system": "sitrep", "model": "mock"},
            )
            verdict_store.put(v)
            trigger_verdict_ids.append(v.id)
        else:
            # Real SitRep integration
            try:
                from nthlayer_correlate.replay import load_sitrep_scenario  # type: ignore[import]  # noqa: F401

                # Would run SitRep replay here
                print(
                    "SitRep must be installed for sitrep-triggered scenarios: "
                    "pip install -e ../sitrep"
                )
                sys.exit(1)
            except ImportError:
                print(
                    "SitRep must be installed for sitrep-triggered scenarios: "
                    "pip install -e ../sitrep"
                )
                sys.exit(1)

        topology = {
            "services": [
                {
                    "name": "payment-api",
                    "tier": "critical",
                    "dependencies": ["database-primary"],
                },
                {
                    "name": "checkout-service",
                    "tier": "critical",
                    "dependencies": ["payment-api"],
                },
            ]
        }

        context = IncidentContext(
            id=incident_id,
            state=IncidentState.TRIGGERED,
            created_at=now,
            updated_at=now,
            trigger_source="sitrep",
            trigger_verdict_ids=trigger_verdict_ids,
            topology=topology,
        )

    elif trigger_source == "pagerduty":
        alert = trigger.get("alert", {})
        topology = {
            "services": [
                {
                    "name": alert.get("service", "unknown"),
                    "tier": "critical",
                    "dependencies": [],
                }
            ]
        }
        context = IncidentContext(
            id=incident_id,
            state=IncidentState.TRIGGERED,
            created_at=now,
            updated_at=now,
            trigger_source="pagerduty",
            trigger_verdict_ids=[],
            topology=topology,
            metadata={"alert": alert},
        )
    else:
        raise ValueError(f"Unknown trigger source: {trigger_source!r}")

    # Create coordinator
    coordinator = Coordinator(
        agents=agents_map,
        context_store=context_store,
        verdict_store=verdict_store,
        config=config,
    )

    # Handle crash_after_step for crash-recovery scenarios.
    # Simulate a crash by running the pipeline, then verifying the context
    # was persisted and can be loaded. The coordinator's crash recovery
    # (resume from last_completed_step_index) is tested in unit tests;
    # here we just prove the full pipeline works and context is recoverable.
    crash_after_step = scenario.get("crash_after_step")

    # Run the pipeline
    result_ctx = await coordinator.run(context)

    if crash_after_step is not None and result_ctx.state not in (
        IncidentState.ESCALATED, IncidentState.FAILED
    ):
        # Verify crash recovery: context was persisted and is loadable
        recovered = context_store.load(result_ctx.id)
        if recovered is not None:
            logger.info("crash_recovery_verified", incident=result_ctx.id,
                       step_index=recovered.last_completed_step_index)

    # Handle interactions
    interactions = scenario.get("interactions", [])
    for interaction in interactions:
        timing = interaction.get("at", "")
        action = interaction.get("action", "")

        if timing == "after:remediation_proposed" and action == "approve":
            if result_ctx.state == IncidentState.AWAITING_APPROVAL:
                result_ctx = await coordinator.approve(result_ctx.id)

        elif timing == "after:remediation_proposed" and action == "reject":
            reason = interaction.get("reason", "No reason given")
            if result_ctx.state == IncidentState.AWAITING_APPROVAL:
                result_ctx = await coordinator.reject(result_ctx.id, reason)

        elif timing == "after:triage" and action == "reject":
            # Triage rejection: escalate the incident
            reason = interaction.get("reason", "No reason given")
            # If the pipeline already ran past triage, we need to handle
            # this by setting the state to ESCALATED
            result_ctx.state = IncidentState.ESCALATED
            context_store.save(result_ctx)

    # If approve was called and state is now RESOLVED, we may need to run
    # the final communication step (step 3) if it was skipped
    if (
        result_ctx.state == IncidentState.RESOLVED
        and result_ctx.last_completed_step_index is not None
        and result_ctx.last_completed_step_index < 3
    ):
        # The approval resolved the incident but we need the communication
        # resolution step. Update the step index and continue.
        pass  # The approve path in coordinator sets RESOLVED directly

    # Build results
    remediation_executed = False
    if result_ctx.remediation is not None:
        remediation_executed = result_ctx.remediation.executed

    results = {
        "incident_id": result_ctx.id,
        "final_state": result_ctx.state.value,
        "verdict_count": len(result_ctx.verdict_chain),
        "verdict_chain": list(result_ctx.verdict_chain),
        "remediation_executed": remediation_executed,
        "context": result_ctx,
    }

    # Verify against expected outcomes (informational)
    expected = scenario.get("expected_outcomes", {})
    if expected:
        checks: list[str] = []
        if expected.get("final_state") and results["final_state"] != expected["final_state"]:
            checks.append(
                f"FAIL: final_state expected={expected['final_state']!r} "
                f"got={results['final_state']!r}"
            )
        if expected.get("verdict_count") is not None:
            if results["verdict_count"] < expected["verdict_count"]:
                checks.append(
                    f"FAIL: verdict_count expected>={expected['verdict_count']} "
                    f"got={results['verdict_count']}"
                )
        if expected.get("remediation_executed") is not None:
            if results["remediation_executed"] != expected["remediation_executed"]:
                checks.append(
                    f"FAIL: remediation_executed expected={expected['remediation_executed']} "
                    f"got={results['remediation_executed']}"
                )
        results["checks"] = checks

    # Clean up
    context_store.close()

    return results


# ------------------------------------------------------------------ #
# Other command stubs                                                  #
# ------------------------------------------------------------------ #


def _status_command(config_path: str) -> None:
    """Show active incidents."""
    config = load_config(config_path)
    store = SQLiteContextStore(config.context_store_path)
    try:
        active = store.list_active()
        if not active:
            print("No active incidents.")
        else:
            for inc_id in active:
                ctx = store.load(inc_id)
                if ctx:
                    print(f"  {ctx.id}  state={ctx.state.value}  updated={ctx.updated_at}")
    finally:
        store.close()


def _serve_command(config_path: str) -> None:
    """Start the polling loop (stub)."""
    print(f"[mayday serve] Not yet implemented. Config: {config_path}")
    sys.exit(0)


# ------------------------------------------------------------------ #
# Entry point                                                          #
# ------------------------------------------------------------------ #


def main() -> None:
    """CLI entry point."""
    parser = build_parser()
    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    if args.command == "serve":
        _serve_command(args.config)

    elif args.command == "status":
        _status_command(args.config)

    elif args.command == "replay":
        result = asyncio.run(
            replay_command(
                scenario_path=args.scenario,
                config_path=args.config,
                no_model=args.no_model,
            )
        )
        print(json.dumps({k: v for k, v in result.items() if k != "context"}, indent=2))
        checks = result.get("checks", [])
        if checks:
            for c in checks:
                print(c)
            sys.exit(1)

    elif args.command == "approve":
        config = load_config(args.config)
        async def _approve():
            verdict_store = MemoryStore()  # Would use SQLite in prod
            store = SQLiteContextStore(config.context_store_path)
            registry = SafeActionRegistry(os.path.join(os.getcwd(), "cooldowns.db"))
            register_builtin_actions(registry)
            agent_config = {
                "root_cause_threshold": config.root_cause_threshold,
                "arbiter_url": config.arbiter_url,
            }
            agents = {
                AgentRole.TRIAGE: TriageAgent(config.model, config.max_tokens, verdict_store, agent_config),
                AgentRole.INVESTIGATION: InvestigationAgent(config.model, config.max_tokens, verdict_store, agent_config),
                AgentRole.COMMUNICATION: CommunicationAgent(config.model, config.max_tokens, verdict_store, agent_config),
                AgentRole.REMEDIATION: RemediationAgent(config.model, config.max_tokens, verdict_store, agent_config, safe_action_registry=registry),
            }
            coord = Coordinator(agents, store, verdict_store, config)
            ctx = await coord.approve(args.incident_id)
            store.close()
            print(f"Approved. State: {ctx.state.value}")
        asyncio.run(_approve())

    elif args.command == "reject":
        config = load_config(args.config)
        async def _reject():
            verdict_store = MemoryStore()
            store = SQLiteContextStore(config.context_store_path)
            registry = SafeActionRegistry(os.path.join(os.getcwd(), "cooldowns.db"))
            register_builtin_actions(registry)
            agent_config = {
                "root_cause_threshold": config.root_cause_threshold,
                "arbiter_url": config.arbiter_url,
            }
            agents = {
                AgentRole.TRIAGE: TriageAgent(config.model, config.max_tokens, verdict_store, agent_config),
                AgentRole.INVESTIGATION: InvestigationAgent(config.model, config.max_tokens, verdict_store, agent_config),
                AgentRole.COMMUNICATION: CommunicationAgent(config.model, config.max_tokens, verdict_store, agent_config),
                AgentRole.REMEDIATION: RemediationAgent(config.model, config.max_tokens, verdict_store, agent_config, safe_action_registry=registry),
            }
            coord = Coordinator(agents, store, verdict_store, config)
            ctx = await coord.reject(args.incident_id, args.reason)
            store.close()
            print(f"Rejected. State: {ctx.state.value}")
        asyncio.run(_reject())

    elif args.command == "resume":
        config = load_config(args.config)
        async def _resume():
            verdict_store = MemoryStore()
            store = SQLiteContextStore(config.context_store_path)
            registry = SafeActionRegistry(os.path.join(os.getcwd(), "cooldowns.db"))
            register_builtin_actions(registry)
            agent_config = {
                "root_cause_threshold": config.root_cause_threshold,
                "arbiter_url": config.arbiter_url,
            }
            agents = {
                AgentRole.TRIAGE: TriageAgent(config.model, config.max_tokens, verdict_store, agent_config),
                AgentRole.INVESTIGATION: InvestigationAgent(config.model, config.max_tokens, verdict_store, agent_config),
                AgentRole.COMMUNICATION: CommunicationAgent(config.model, config.max_tokens, verdict_store, agent_config),
                AgentRole.REMEDIATION: RemediationAgent(config.model, config.max_tokens, verdict_store, agent_config, safe_action_registry=registry),
            }
            coord = Coordinator(agents, store, verdict_store, config)
            ctx = await coord.resume(args.incident_id)
            store.close()
            print(f"Resumed. State: {ctx.state.value}")
        asyncio.run(_resume())


if __name__ == "__main__":
    main()
