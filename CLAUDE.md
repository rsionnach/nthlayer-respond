# Mayday

<!-- AUTO-MANAGED: project-description -->
## Project Description

Multi-agent incident response system coordinated by AI. Agents collaborate to triage, investigate, communicate, and remediate incidents under human supervision. Mayday owns the incident lifecycle; PagerDuty, Slack, and email are notification channels it uses, not upstream incident sources.

- Status: Phase 3 implemented — all agents, coordinator, CLI, and 8 scenario fixtures complete
- Beads epic: opensrm-m50 (centralized in opensrm repo; supersedes mayday-bel)
- Demo milestone: Demo 3 "The Full Chain" — SitRep correlation verdicts feed Mayday pipeline, verdict lineage chain complete
- Accept criteria: `mayday replay --scenario scenarios/synthetic/cascading-failure.yaml --no-model` produces 5 verdicts with full lineage, final state RESOLVED
- Design spec: `opensrm/docs/superpowers/specs/2026-03-19-phase-3-mayday-implementation-design.md`
- License: Apache 2.0
- Contributing: see CONTRIBUTING.md

## Build Commands

- **Install dependencies (including verdict library):** `uv sync --extra dev`
- **Run tests:** `uv run --extra dev pytest tests/ -v`
- **Run single test file:** `uv run --extra dev pytest tests/test_types.py -v`
- **Run CLI:** `uv run --extra dev mayday <serve|status|replay|approve|reject|resume>`
- **Replay scenario:** `uv run --extra dev mayday replay --scenario scenarios/synthetic/cascading-failure.yaml --no-model`
- **Resume crashed incident:** `uv run --extra dev mayday resume <incident_id>`
- **Approve/reject remediation:** `uv run --extra dev mayday approve <incident_id>` / `mayday reject <incident_id> --reason <reason>`
- **TDD workflow:** write failing test → implement → `uv run --extra dev pytest` verify pass → commit
<!-- END AUTO-MANAGED -->

<!-- AUTO-MANAGED: architecture -->
## Architecture

Follows [Zero Framework Cognition](ZFC.md): the orchestrator is pure transport; agents provide judgment.

**Source layout (`src/mayday/`):**
- `cli.py` — 6 subcommands: serve, status, replay, approve, reject, resume
- `coordinator.py` — deterministic state machine; sequences agent pipeline; persists IncidentContext to SQLite
- `types.py` — IncidentContext, TriageResult, InvestigationResult, RemediationResult, CommunicationResult, Hypothesis
- `config.py` — MaydayConfig, load_config
- `context_store.py` — SQLiteContextStore (crash recovery persistence)
- `agents/base.py` — AgentBase ABC; transport layer (model calls, verdict emission, governance HTTP)
- `agents/triage.py`, `investigation.py`, `remediation.py`, `communication.py` — concrete agents
- `safe_actions/registry.py` — SafeActionRegistry (closed callable registry, cooldown tracking)
- `safe_actions/actions.py` — register_builtin_actions (rollback, scale_up, disable_feature_flag, reduce_autonomy)

**Scenario fixtures (`scenarios/synthetic/`):**
8 YAML replay fixtures: cascading-failure, autonomy-reduction, crash-recovery, human-override, low-confidence-escalation, model-unavailable, remediation-approval, sitrep-unavailable. Each has `mock_responses` keyed by: triage, investigation, communication_initial, remediation, communication_resolution.

**Orchestrator** (deterministic state machine, not an agent):
- Receives incident trigger, creates shared context, sequences agent pipeline, routes messages
- Degrades to "no AI opinion" (not "no incident response") when model is unavailable

**Agent pipeline** (sequencing):
1. Triage (first)
2. Investigation + Communication (parallel)
3. Remediation + Communication update (after root cause found)

**Shared Incident Context** — single accumulating object all agents read/write:
- `triage`: severity, blast_radius, affected_slos, assigned_teams
- `investigation`: hypotheses (with confidence scores), root_cause
- `communication`: updates_sent (channel, timestamp, type)
- `remediation`: proposed_action, target, risk_assessment, requires_human_approval

**Agent roles and judgment SLOs:**

| Agent | Authority | Cannot | Judgment SLO |
|-------|-----------|--------|--------------|
| Triage | Set severity, notify teams, assign ownership | Remediate; override classification without human approval | Reversal rate < 10% |
| Investigation | Form/rank hypotheses; declare root cause above confidence threshold | Execute any remediation | Root cause agreement with post-incident review, target 70% at maturity |
| Communication | Draft/send updates within pre-approved templates; choose channels and timing | Contradict investigation findings; communicate resolution until confirmed | Human edit rate < 15% |
| Remediation | Suggest fixes; execute pre-approved safe actions (rollback, scale up, disable feature flag) | Execute novel actions not pre-approved in OpenSRM manifest; touch services outside blast radius | Fix success rate 80% |

**Alert flow:**
```
Alert Source (Arbiter quality breach / Prometheus alert / any webhook)
→ SitRep Snapshot (correlated context)
→ Mayday Orchestrator (creates incident context)
→ Agent Pipeline (triage → investigate + communicate → remediate)
→ Notification Channels (PagerDuty / Slack / email / status page)
```
<!-- END AUTO-MANAGED -->

<!-- AUTO-MANAGED: patterns -->
## Key Design Patterns

**ZFC transport/judgment split for Mayday:**
- Transport (code): receiving alerts, sequencing agent execution, routing messages, persisting incident context
- Judgment (model): triaging severity, forming hypotheses, assessing risk, drafting communications

**Human-in-the-loop:**
- Agents never take destructive action without human approval unless the action is pre-approved as safe in the OpenSRM manifest
- Every human override feeds back into that agent's judgment SLO
- Arbiter uses a one-way safety ratchet: can reduce agent autonomy but cannot increase it without human approval

**Safe action conditions — closed registry, not expression language:**
- Each condition is a registered callable (named function)
- Unknown condition names fail at startup — no runtime eval of arbitrary expressions
- Cooldown and blast radius checks built into the registry

**Autonomy reduction (first-class concept):**
- When an AI agent's model update causes a quality breach, the remediation agent (or triage agent) can request the Arbiter to reduce that agent's autonomy
- `reduce_autonomy` is a built-in safe action in the registry, targeting `POST {arbiter_url}/api/v1/governance/reduce`
- Unlike other safe actions (which target infrastructure), this targets another ecosystem component
- The Arbiter's one-way safety ratchet means reduction always succeeds; restoration requires a separate human action
- Trigger condition in TriageAgent._post_execute: any trigger verdict has tag `"agent_model_update"` AND `result.severity <= 2`

**Approval ratchet:**
- The model can escalate approval requirements (request human sign-off) but never downgrade them
- If a safe action's default is `requires_approval=True`, the model cannot override it to False
- Same principle as the Arbiter's autonomy ratchet — one-way safety mechanisms

**Crash recovery:**
- `IncidentContext` serialised to SQLite after each pipeline step
- Coordinator resumes from `last_completed_step_index` (int, not AgentRole — disambiguates parallel steps)
- Testable with mock agents (no model required)

**AgentBase transport layer (all agents inherit):**
- `_call_model`: lazy Anthropic client init, `asyncio.to_thread` for blocking SDK call, `asyncio.wait_for` timeout
- `_emit_verdict`: sets `subject.type=role.value`, wires `lineage.context` from `context.trigger_verdict_ids`, chains `lineage.parent` from `context.verdict_chain[-1]`
- `_degraded_verdict`: emits `confidence=0.0`, `action="escalate"`, tags `["degraded","human-takeover-required"]` when model fails
- `_parse_json`: strips markdown fences and preamble before parsing; handles `{...}` extraction from noisy model output
- `_request_autonomy_reduction`: stdlib `urllib`, 3 retries, POST to Arbiter governance endpoint

**Two-phase communication:**
- Phase 1 (called when `context.remediation is None`): drafts initial status update
- Phase 2 (called after remediation): drafts resolution update; APPENDS to Phase 1's `updates_sent` — never replaces

**Post-incident learning loop:**
- Manifest updates (tighter SLO targets, new dependency declarations, new safe action definitions)
- NthLayer alerting rule refinements from quality patterns
- Arbiter judgment SLO threshold revisions from historical data
- SitRep correlation improvements from past incident accuracy
<!-- END AUTO-MANAGED -->

<!-- AUTO-MANAGED: verdict-integration -->
## Verdict Integration

All agents produce verdicts via `AgentBase._emit_verdict`. Consuming SitRep correlation verdicts as trigger input is implemented (TriageAgent and InvestigationAgent read from `context.trigger_verdict_ids`). See `verdicts/` and `VERDICT-INTEGRATION.md`.

**Install:** verdict is a path dependency managed via `uv.sources` (`uv sync` installs it automatically from `../verdicts/lib/python`)

**Shared store:** single `verdicts.db` with WAL mode — NOT a per-component store. All ecosystem components read/write the same file.

**Consuming SitRep verdicts (implemented — Triage and Investigation agents read trigger_verdict_ids):**
```python
sitrep_verdicts = verdict_store.query(VerdictFilter(
    producer_system="sitrep", subject_type="correlation",
    from_time=last_30_minutes, limit=0,
))
# verdicts provide: timestamp (staleness), confidence (trust level), lineage (provenance)
```

**Verdicts produced per agent role (implemented):**
- Triage Agent → `subject.type: "triage"` verdict (severity, blast radius)
- Investigation Agent → `subject.type: "investigation"` verdict (hypotheses, root cause)
- Communication Agent → `subject.type: "communication"` verdict (status update content)
- Remediation Agent → `subject.type: "remediation"` verdict (proposed fix, rollback decision)
- `"escalation"` and `"incident_summary"` types map to `"custom"` with `metadata.custom["incident_type"]`

All Mayday verdicts include `lineage.context = [sitrep_verdict.id, ...]` linking to the SitRep verdicts that informed them.

**Lineage chain:** SitRep correlation verdicts → Mayday triage verdict → investigation verdict → remediation verdict → human override verdict. One human override at any point calibrates every component upstream via lineage traversal.

**Degraded mode:** Agents still produce verdicts with `confidence: 0.0` and a note in `reasoning` when operating on stale SitRep data or without model access.

**Verdict config:**
```yaml
verdict:
  store:
    backend: sqlite
    path: verdicts.db
```
<!-- END AUTO-MANAGED -->

<!-- AUTO-MANAGED: git-insights -->
## Ecosystem Integration

**Reads from OpenSRM manifests:**
- Severity tiers and SLO targets (Triage Agent)
- Safe action definitions for auto-execution (Remediation Agent)
- Dependency topology — which services to check (Investigation Agent)
- Escalation paths and ownership metadata (Communication Agent)

**Ecosystem dependencies:**
- **SitRep** — correlated snapshots as starting context for every incident
- **Arbiter** — quality scores for agent reliability; governance layer adjusts Mayday agent autonomy
- **NthLayer** — topology exports, deployment gate status; consumes Mayday post-incident findings

**OpenSRM ecosystem** (each component works standalone, composes via shared manifests + OTel):
- [OpenSRM](https://github.com/rsionnach/opensrm) — service reliability specification
- [Verdict](../verdicts/) — data primitive; Mayday consumes SitRep's verdicts and produces its own per agent role
- [Arbiter](https://github.com/rsionnach/arbiter) — quality measurement and AI agent governance
- [NthLayer](https://github.com/rsionnach/nthlayer) — generate monitoring infrastructure from manifests
- [SitRep](https://github.com/rsionnach/sitrep) — situational awareness through signal correlation
- Mayday (this repo) — multi-agent incident response
<!-- END AUTO-MANAGED -->
