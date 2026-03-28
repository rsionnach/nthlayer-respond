# nthlayer-respond

<!-- AUTO-MANAGED: project-description -->
## Project Description

Multi-agent incident response system coordinated by AI. Agents collaborate to triage, investigate, communicate, and remediate incidents under human supervision. nthlayer-respond owns the incident lifecycle; PagerDuty, Slack, and email are notification channels it uses, not upstream incident sources.

- Status: Phase 3 implemented — all agents, coordinator, CLI, and 8 scenario fixtures complete
- Beads epic: opensrm-m50 (centralized in opensrm repo; supersedes mayday-bel)
- Demo milestone: Demo 3 "The Full Chain" — nthlayer-correlate correlation verdicts feed nthlayer-respond pipeline, verdict lineage chain complete
- Accept criteria: `nthlayer-respond replay --scenario scenarios/synthetic/cascading-failure.yaml --no-model` produces 5 verdicts with full lineage, final state RESOLVED
- Design spec: `opensrm/docs/superpowers/specs/2026-03-19-phase-3-mayday-implementation-design.md`
- License: Apache 2.0
- Contributing: see CONTRIBUTING.md

## Build Commands

- **Install dependencies (including nthlayer-learn + nthlayer-common path deps):** `uv sync --extra dev`
- **Run tests:** `uv run --extra dev pytest tests/ -v`
- **Run single test file:** `uv run --extra dev pytest tests/test_types.py -v`
- **Run CLI:** `uv run --extra dev nthlayer-respond <serve|status|replay|approve|reject|resume|respond>`
- **Live respond from correlation verdict:** `uv run --extra dev nthlayer-respond respond --trigger-verdict <id> --specs-dir <dir> --verdict-store verdicts.db`
- **Replay scenario:** `uv run --extra dev nthlayer-respond replay --scenario scenarios/synthetic/cascading-failure.yaml --no-model`
- **Resume crashed incident:** `uv run --extra dev nthlayer-respond resume <incident_id>`
- **Approve/reject remediation:** `uv run --extra dev nthlayer-respond approve <incident_id>` / `nthlayer-respond reject <incident_id> --reason <reason>`
- **TDD workflow:** write failing test → implement → `uv run --extra dev pytest` verify pass → commit
<!-- END AUTO-MANAGED -->

<!-- AUTO-MANAGED: architecture -->
## Architecture

Follows [Zero Framework Cognition](ZFC.md): the orchestrator is pure transport; agents provide judgment.

**Source layout (`src/nthlayer_respond/`):**
- `cli.py` — 7 subcommands: serve, status, replay, approve, reject, resume, respond
- `coordinator.py` — deterministic state machine; sequences agent pipeline; persists IncidentContext to SQLite
- `types.py` — IncidentContext, TriageResult, InvestigationResult, RemediationResult, CommunicationResult, Hypothesis
- `config.py` — RespondConfig, load_config
- `context_store.py` — SQLiteContextStore (crash recovery persistence)
- `agents/base.py` — AgentBase ABC; transport layer (model calls, verdict emission, governance HTTP)
- `agents/triage.py`, `investigation.py`, `remediation.py`, `communication.py` — concrete agents
- `safe_actions/registry.py` — SafeActionRegistry (closed callable registry, cooldown tracking)
- `safe_actions/actions.py` — register_builtin_actions (rollback, scale_up, disable_feature_flag, reduce_autonomy, pause_pipeline)

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

| Agent | Authority | Cannot | Judgment SLO | default_timeout |
|-------|-----------|--------|--------------|-----------------|
| Triage | Set severity (0–4, 0=P0), notify teams, assign ownership | Remediate; override classification without human approval | Reversal rate < 10% | 15s |
| Investigation | Form/rank hypotheses; declare root cause above confidence threshold | Execute any remediation | Root cause agreement with post-incident review, target 70% at maturity | 60s |
| Communication | Draft/send updates within pre-approved templates; choose channels and timing | Contradict investigation findings; communicate resolution until confirmed | Human edit rate < 15% | 20s |
| Remediation | Suggest fixes; execute pre-approved safe actions (rollback, scale_up, disable_feature_flag, reduce_autonomy, pause_pipeline) | Execute novel actions not pre-approved in OpenSRM manifest; touch services outside blast radius | Fix success rate 80% | 30s |

**Alert flow:**
```
Alert Source (nthlayer-measure quality breach / Prometheus alert / any webhook)
→ nthlayer-correlate Snapshot (correlated context)
→ nthlayer-respond respond --trigger-verdict <id>  ← live trigger path
→ nthlayer-respond Orchestrator (creates incident context)
→ Agent Pipeline (triage → investigate + communicate → remediate)
→ Notification Channels (PagerDuty / Slack / email / status page)
```

**`respond` subcommand (live trigger from correlation verdict):**
- `nthlayer-respond respond --trigger-verdict <id> --specs-dir <dir> --verdict-store <path> [--notify stdout|<webhook-url>] [--model <provider/model>]`
- Reads correlation verdict from store; builds `IncidentContext` with `trigger_verdict_ids=[corr.id]`, `trigger_source="nthlayer-correlate"`
- Severity mapped from verdict confidence: `> 0.8 → 1` (critical), `> 0.5 → 2` (major), else `3` (minor)
- Incident ID format: `INC-{SERVICE_UPPER}-{timestamp}`
- Topology loaded from `--specs-dir` OpenSRM YAMLs
- Exits 1 if trigger verdict not found

**`replay` subcommand — `--no-model` scenario execution:**
- `replay_command()` is async; accepts `work_dir` parameter for temp directory override; uses `MemoryStore` (not `SQLiteVerdictStore`) for verdicts in replay; auto-creates tempdir if `work_dir=None`
- `_build_replay_agents()`: patches `_call_model` per agent when `--no-model`; mock responses keyed by `triage`, `investigation`, `communication_initial`, `communication_resolution`, `remediation`; communication agent uses sequenced mock (2-call: initial then resolution); all 4 agents accept `timeout=` kwarg from config (`triage_timeout`, `investigation_timeout`, `communication_timeout`, `remediation_timeout`)
- `replay --no-model` registry override: sets `requires_approval=False` on the named action ONLY when `scenario.mock_responses.remediation.requires_human_approval` is explicitly `false`; never overrides to `True` (approval ratchet preserved)
- `_build_incident_context()` for `nthlayer-correlate` trigger source (also accepts legacy `"sitrep"` alias in scenario YAMLs): in `--no-model` mode, creates a mock correlation verdict via `verdict_create()` and puts it in `MemoryStore`; hardcoded topology: payment-api (critical, depends database-primary) + checkout-service (critical, depends payment-api); sets `trigger_source="nthlayer-correlate"` regardless of alias used; for `pagerduty` trigger: builds single-service topology from `trigger.alert.service` with empty dependencies list
- `_handle_interactions()`: processes scenario `interactions[]` entries — supports `after:remediation_proposed/approve`, `after:remediation_proposed/reject`, `after:triage/reject`
<!-- END AUTO-MANAGED -->

<!-- AUTO-MANAGED: patterns -->
## Key Design Patterns

**ZFC transport/judgment split for nthlayer-respond:**
- Transport (code): receiving alerts, sequencing agent execution, routing messages, persisting incident context
- Judgment (model): triaging severity, forming hypotheses, assessing risk, drafting communications

**Human-in-the-loop:**
- Agents never take destructive action without human approval unless the action is pre-approved as safe in the OpenSRM manifest
- Every human override feeds back into that agent's judgment SLO
- nthlayer-measure uses a one-way safety ratchet: can reduce agent autonomy but cannot increase it without human approval

**Safe action conditions — closed registry, not expression language:**
- Each condition is a registered callable (named function)
- Unknown condition names fail at startup — no runtime eval of arbitrary expressions
- Cooldown and blast radius checks built into the registry
- `SafeAction.blast_radius_check` signature: `(target: str, context: IncidentContext) -> bool` — the full context is passed at call time; the inline comment in `registry.py` still says `topology_dict` (stale — ignore it)
- `SafeActionRegistry.execute()` supports both sync and async handlers (detected via `inspect.iscoroutinefunction`)

**Autonomy reduction (first-class concept):**
- When an AI agent's model update causes a quality breach, the remediation agent (or triage agent) can request the nthlayer-measure to reduce that agent's autonomy
- `reduce_autonomy` is a built-in safe action in the registry, targeting `POST {arbiter_url}/api/v1/governance/reduce`
- Unlike other safe actions (which target infrastructure), this targets another ecosystem component
- The nthlayer-measure's one-way safety ratchet means reduction always succeeds; restoration requires a separate human action
- Trigger condition in TriageAgent._post_execute: any trigger verdict has tag `"agent_model_update"` AND `result.severity <= 2`

**Approval ratchet:**
- The model can escalate approval requirements (request human sign-off) but never downgrade them
- If a safe action's default is `requires_approval=True`, the model cannot override it to False
- Same principle as the nthlayer-measure's autonomy ratchet — one-way safety mechanisms

**Crash recovery:**
- `IncidentContext` serialised to SQLite after each pipeline step
- Coordinator resumes from `last_completed_step_index` (int, not AgentRole — disambiguates parallel steps)
- Testable with mock agents (no model required)

**AgentBase transport layer (all agents inherit):**
- `_call_model`: delegates to `nthlayer_common.llm.llm_call` via `asyncio.to_thread` + `asyncio.wait_for` timeout; model format `"provider/model"` (anthropic, openai, ollama, etc.)
- `_emit_verdict`: sets `subject.type=role.value`, wires `lineage.context` from `context.trigger_verdict_ids`, chains `lineage.parent` from `context.verdict_chain[-1]`
- `_build_service_context_prompt`: builds a service context section for agent prompts from `context.metadata.service_context`; emits service name, service_type (labelled "AI decision service" or "traditional service"), tier, team, breached SLO name/type with description ("JUDGMENT SLO — measures decision quality" vs "measures infrastructure reliability"), current/target values, declared SLOs list, and role-specific remediation guidance (AI gate: model rollback/canary revert/autonomy reduction; infra: rollback/scale_up/restart/feature flag disable); returns `""` if no `service_context` key present
- `_degraded_verdict`: emits `confidence=0.0`, `action="escalate"`, tags `["degraded","human-takeover-required"]` when model fails; delegates subject summary to `_build_degraded_summary`
- `_build_degraded_summary`: constructs informative degraded subject summary from `context.metadata` — extracts `blast_radius`, `root_causes[0].service/type`, `severity`, `incident_id`; role-specific format: triage → `"DEGRADED: SEV-N — service type, K services in blast radius"`; investigation → `"DEGRADED: Manual investigation required — root cause from correlation: service (type)"`; communication → `"DEGRADED: Draft status update required for {incident_id}"`; remediation → `"DEGRADED: Manual remediation required — see correlation verdict for recommended actions"`
- `_parse_json`: strips markdown fences and preamble before parsing; handles `{...}` extraction from noisy model output via brace-depth matching
- `_build_summary`: role-specific subject summary for emitted verdicts — triage: `"SEV-{sev}: {first sentence of reasoning}"` or fallback `"SEV-{sev} — {N} services in blast radius[, assigned to {team}]"`; investigation: `"Root cause ({confidence:.0%} confidence): {rc[:90]}"` or `"Hypothesis: {desc[:90]}"` or first sentence of reasoning ([:90]) or `"Agent response produced no summary — see raw output"` (with `log.warning`); communication: `"{'via ' + channel + ': ' if channel else ''}{content[:90]}"` (channel omitted if empty) or first sentence of reasoning ([:90]) or generic `"Agent response produced no summary — see raw output"` (no communication-specific context fallback); remediation: `"{action} on {target}[ (requires approval)]"` (approval suffix only when `requires_human_approval=True`; no auto-approved text; no risk suffix) or `"Proposed: {action}"` (no approval suffix on action-only branch) or first sentence of reasoning ([:90]) or context-based fallback from `metadata.root_causes`
- `execute()` template method: calls `build_prompt` → `_call_model` → `parse_response` → `_apply_result` → `_build_summary` → `_emit_verdict(action="flag")` → `_post_execute`; on any exception emits `_degraded_verdict` instead
- `_post_execute(context, result) -> IncidentContext`: hook called after successful execute cycle; no-op in AgentBase; overridden by subclasses (e.g. TriageAgent triggers autonomy reduction here)
- `_request_autonomy_reduction`: stdlib `urllib`, 3 retries, POST to nthlayer-measure governance endpoint

**Two-phase communication:**
- Phase 1 (called when `context.remediation is None`): drafts initial status update
- Phase 2 (called after remediation): drafts resolution update; APPENDS to Phase 1's `updates_sent` — never replaces
- `_apply_result`: if `context.communication is None` sets it; otherwise appends `updates_sent` entries and updates `reasoning` — phase 1 updates are preserved
- Flat-field synthesis fallback (when model returns no `updates`/`messages` array): joins non-empty fields `title`, `impact_description`, `current_status`, `summary`, `message` with `" — "`; `channel` defaults to `"status_page"`, `update_type` read from `data.get("status", "initial")` (key is `"status"`, not `"update_type"`)

**parse_response field aliases (each agent accepts multiple JSON key names):**
- Triage: `severity` (clamped to [0,4]); `blast_radius` (coerced to list if scalar); `assigned_team` or `team_assignment`; `reasoning` or `rationale`
- Investigation: hypotheses items accept `description`, `hypothesis`, or `summary`; `evidence` falls back to `reasoning` field if empty; `root_cause_confidence` also accepts top-level `confidence`; `reasoning` or `analysis`
- Remediation: `proposed_action`, `recommended_action`, or `action`; `target` or `target_service`; `risk_assessment` or `risk`; `reasoning` or `rationale`; `autonomy_reduction` dict stashed on result for `_post_execute`
- Communication: top-level list key `updates` or `messages`; per-item `update_type` or `type`; per-item `content` or `message`; `reasoning` or `rationale`

**Remediation `_post_execute` two-step sequence:**
1. Execute safe action if `not result.requires_human_approval` and `result.proposed_action is not None` → sets `result.executed: bool`, `result.execution_result: str`
2. Autonomy reduction if `autonomy_reduction.recommended` → POSTs to `arbiter_url`; sets `result.autonomy_reduced: bool`, `result.autonomy_target: str`, `result.previous_autonomy_level`, `result.new_autonomy_level`

**Post-incident learning loop:**
- Manifest updates (tighter SLO targets, new dependency declarations, new safe action definitions)
- NthLayer alerting rule refinements from quality patterns
- nthlayer-measure judgment SLO threshold revisions from historical data
- nthlayer-correlate correlation improvements from past incident accuracy
<!-- END AUTO-MANAGED -->

<!-- AUTO-MANAGED: verdict-integration -->
## Verdict Integration

All agents produce verdicts via `AgentBase._emit_verdict`. Consuming nthlayer-correlate correlation verdicts as trigger input is implemented (TriageAgent and InvestigationAgent read from `context.trigger_verdict_ids`). See `nthlayer-learn/` for the verdict library.

**Install:** both `nthlayer-learn` and `nthlayer-common` are path dependencies managed via `uv.sources` (`uv sync` installs them automatically from `../nthlayer-learn/lib/python` and `../nthlayer-common`)

**Shared store:** single `verdicts.db` with WAL mode — NOT a per-component store. All ecosystem components read/write the same file.

**Consuming nthlayer-correlate verdicts (implemented — Triage and Investigation agents read trigger_verdict_ids):**
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

All nthlayer-respond verdicts include `lineage.context = [sitrep_verdict.id, ...]` linking to the nthlayer-correlate verdicts that informed them.

**Lineage chain:** nthlayer-correlate correlation verdicts → nthlayer-respond triage verdict → investigation verdict → remediation verdict → human override verdict. One human override at any point calibrates every component upstream via lineage traversal.

**Coordinator approve/reject verdict behaviour:**
- `approve(incident_id)`: after executing the safe action, emits a `"remediation"` verdict with `action="approve"`, `confidence=1.0`, `reasoning="Human approved {action} on {target}"`; appends to `context.verdict_chain`; state → RESOLVED. On execution failure: emits `action="escalate"`, `confidence=0.0`; state → ESCALATED.
- `reject(incident_id, reason)`: resolves the last verdict in `context.verdict_chain` as `"overridden"` with `override={"by": "human"}`; state → ESCALATED.

**Degraded mode:** Agents still produce verdicts with `confidence: 0.0` and a note in `reasoning` when operating on stale nthlayer-correlate data or without model access.

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
- **nthlayer-correlate** — correlated snapshots as starting context for every incident
- **nthlayer-measure** — quality scores for agent reliability; governance layer adjusts nthlayer-respond agent autonomy
- **NthLayer** — topology exports, deployment gate status; consumes nthlayer-respond post-incident findings

**OpenSRM ecosystem** (each component works standalone, composes via shared manifests + OTel):
- [opensrm](../opensrm/) — service reliability specification
- [nthlayer-learn](../nthlayer-learn/) — data primitive; nthlayer-respond consumes nthlayer-correlate's verdicts and produces its own per agent role
- [nthlayer-measure](../nthlayer-measure/) — quality measurement and AI agent governance
- [nthlayer](../nthlayer/) — generate monitoring infrastructure from manifests
- [nthlayer-correlate](../nthlayer-correlate/) — situational awareness through signal correlation
- nthlayer-respond (this repo) — multi-agent incident response
<!-- END AUTO-MANAGED -->
