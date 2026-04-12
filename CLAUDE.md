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
- `coordinator.py` — deterministic state machine; sequences agent pipeline; persists IncidentContext to SQLite; optional `escalation_runner` parameter (backward-compatible — omit for no escalation); `_maybe_start_escalation()` called after triage step (step 0), parses `oncall.escalation` list from `context.metadata["service_context"]["spec"]["ownership"]["oncall"]`, converts `"Nm"` after strings to `timedelta(minutes=N)`, builds `NotificationPayload` from triage result, fires `EscalationRunner.start_escalation()`; fail-open — escalation failure logs warning and never blocks the incident pipeline
- `types.py` — IncidentContext, TriageResult, InvestigationResult, RemediationResult, CommunicationResult, Hypothesis; all result types have `confidence: float | None = None` — `None` means "not yet parsed from model response" (not a hardcoded fallback value)
- `config.py` — RespondConfig, load_config. `RespondConfig.model` defaults to `NTHLAYER_MODEL` env var if set, else `claude-sonnet-4-20250514`. Slack fields: `slack_signing_secret`, `slack_bot_token` (both default `""`); loaded from `slack:` YAML section. Notification backend fields: `ntfy_server_url`, `ntfy_auth_token`, `twilio_account_sid`, `twilio_auth_token`, `twilio_from_number`, `pagerduty_routing_key`, `webhook_base_url` (default `http://localhost:8090`); loaded from `notifications:` YAML section (`notifications.ntfy.*`, `notifications.twilio.*`, `notifications.pagerduty.routing_key`, `notifications.webhook.public_url`).
- `context_store.py` — SQLiteContextStore (crash recovery persistence)
- `server.py` — `ApprovalServer` (Starlette ASGI): routes for approve/reject/status + Slack interaction callback + `GET /metrics`; `verdict_store` param wires `VerdictMetricsCollector` for Prometheus scraping; approval timeout tracking with auto-reject; `recover_pending_approvals()` restores timeouts on startup
- `metrics.py` — `VerdictMetricsCollector`: queries verdict store; emits plain Prometheus text exposition (no prometheus_client dep); gauges: `nthlayer_verdicts_total`, `nthlayer_verdict_accuracy` (1 - reversal rate), `nthlayer_verdict_reversal_rate`; labels: `component` (strips "nthlayer-" prefix), `verdict_type`, `window` (7d/30d); accuracy/reversal gauges omitted when no resolved verdicts in window
- `agents/base.py` — AgentBase ABC; transport layer (model calls, verdict emission, governance HTTP, Slack notifications)
- `agents/triage.py`, `investigation.py`, `remediation.py`, `communication.py` — concrete agents
- `notifications.py` — Slack block builders and `send_slack_notification()` async helper; `should_notify(context, event_type, severity=None)` checks `spec.notifications.events` filter (absent = allow all; severity filter [0-4] optional per entry); `resolve_slack_channel` resolution order: spec.notifications.slack.channel_id → spec.ownership.slack_channel → SLACK_CHANNEL_ID env → None; opt-in via `SLACK_WEBHOOK_URL`
- `safe_actions/registry.py` — SafeActionRegistry (closed callable registry, cooldown tracking)
- `safe_actions/actions.py` — YAML-driven registration: `load_safe_action_policy()` reads `registry/safe-actions.yaml`; `register_builtin_actions()` checks `binding` field — uses `_make_webhook_handler(binding)` if binding is present and not `"stub"`, else falls back to `_HANDLERS` stub; actions with no handler and no binding emit `logger.warning` and are skipped
- `safe_actions/webhook.py` — `WebhookDispatcher` class (`execute`, `_call_webhook`, `_verify`); `ExecutionResult` dataclass; `render_binding_templates()` and `resolve_secrets()` module-level utilities
- `notification_backends/__init__.py` — package: "Notification delivery backends for on-call escalation."
- `notification_backends/protocol.py` — `NotificationBackend` Protocol (async `send(recipient, payload) -> NotificationResult`; async `health_check() -> bool`); `NotificationPayload` dataclass (incident_id, severity int 1=P1, title, summary, root_cause, blast_radius, actions_url, escalation_step, requires_ack); `NotificationResult` dataclass (delivered, channel "slack_dm"|"ntfy"|"phone"|"pagerduty"|"stdout", recipient, timestamp, message_id, error); adding a new channel = one file implementing this protocol
- `notification_backends/ntfy_backend.py` — `NtfyNotificationBackend`: DND-override push notifications; each roster member has a personal ntfy topic; `PRIORITY_MAP` {1: "max", 2: "urgent", 3: "high", 4: "default"}, unknown → "high"; `TAGS_MAP` severity → emoji tags; config: `server_url` (env: NTFY_SERVER_URL, default https://ntfy.sh), `webhook_base_url` (env: NTHLAYER_WEBHOOK_URL, default http://localhost:8090), `timeout=10.0`; `NTFY_AUTH_TOKEN` env var → Bearer auth; accepts optional injected `client` (httpx.AsyncClient), `_owns_client` flag for `close()`; `send()` returns error result if recipient has no `ntfy_topic`; ack header `Actions: http, Acknowledge, {webhook_base_url}/api/v1/incidents/{id}/ack, method=POST, clear=true`; message_id from response JSON `.id`; `health_check()`: GET `{server_url}/v1/health`
- `notification_backends/slack_backend.py` — `SlackNotificationBackend`: Slack DM and channel notifications via Block Kit; `SEVERITY_EMOJI` {1: red_circle, 2: orange, 3: yellow, 4: blue}; `__init__(client)` accepts SlackWebClient-compatible object; `send()`: DM via `client.post_message(channel=recipient.slack_id, blocks=..., text=fallback)`; `send_to_channel(channel, payload)`: posts with `<!here>` mention; `_build_incident_blocks()`: header (emoji + incident_id + title, max 150 chars), optional @here, summary, optional root_cause, optional blast_radius (backtick-formatted services), optional actions block when `requires_ack=True`; action_id values: `"incident_ack"` (primary) and `"incident_escalate"` (danger) — distinct from approval flow in server.py which uses `"approve"` / `"reject"`
- `notification_backends/stdout_backend.py` — `StdoutNotificationBackend`: for testing and local development; `SEVERITY_LABEL` {1: "P1 CRITICAL", 2: "P2 MAJOR", 3: "P3 MINOR", 4: "P4 INFO"}; prints formatted incident info; omits root_cause/blast_radius sections when None/empty; `health_check()` always True
- `oncall/__init__.py` — package: "On-call schedule resolution and escalation engine."
- `oncall/schedule.py` — pure function on-call resolver (no state, no database); `RosterMember` dataclass (name, slack_id, ntfy_topic?, phone?) — intentionally separate from `nthlayer.specs.manifest.RosterMember` (build-time schema); extraction to nthlayer-common planned when API stabilises; `OnCallResult` dataclass (primary, secondary, rotation_handoff, source "rotation"|"override"); secondary equals primary for single-person rosters; `DAY_MAP` lookup dict for day names (no regex, per project convention); `resolve_oncall(oncall_config: dict, now: datetime) -> OnCallResult` — checks overrides first, then computes rotation position via stable epoch (2000-01-03 Monday); supports "weekly" and "daily" rotation types and "monday 09:00" / "09:00" handoff formats; oncall_config shape: `{timezone, rotation: {type, handoff, roster: [{name, slack_id, ntfy_topic?, phone?}]}, overrides?: [{start, end, user, reason?}]}`; override boundary: start inclusive, end exclusive; secondary always wraps to roster[0] when primary is last; `_rotation_period()`: lookup dict, raises ValueError for unknown type; `_parse_handoff()`: parses "day HH:MM" or "HH:MM"; validates hour [0-23] and minute [0-59]; raises ValueError for empty roster, unknown override user, invalid rotation type/handoff
- `oncall/escalation.py` — pure data + methods, no I/O, no async; `EscalationStatus` enum (ACTIVE, ACKNOWLEDGED, EXHAUSTED, RESOLVED); `EscalationStep` dataclass (after: timedelta, notify: str, target: str|None, phone: str|None); `EscalationState` dataclass (incident_id, started_at, steps, current_step_index=0, acknowledged_by, acknowledged_at, status=ACTIVE, notifications_sent[]); `acknowledge(user, at)` sets status=ACKNOWLEDGED and stops all further steps; `resolve()` sets status=RESOLVED; `next_due_step(now)` returns next step when `now >= started_at + step.after`, advances current_step_index, sets EXHAUSTED when all steps consumed; `time_until_next_step(now)` returns remaining timedelta or None (for polling loop sleep calculation); driven externally by EscalationRunner
- `oncall/runner.py` — `EscalationRunner`: drives the EscalationState machine and dispatches to notification backends; `__init__(backends: dict[str, NotificationBackend], oncall_config: dict, slack_channel: str|None)`; `start_escalation(incident_id, payload, steps) -> EscalationState`: fires immediately-due steps (after=0m) synchronously then starts `_run_loop` background asyncio.Task for remaining steps; `acknowledge(incident_id, user)`: marks ACKNOWLEDGED and cancels background task; `_execute_step(state, step, payload)`: resolves on-call via `resolve_oncall()`; target routing: `"next_oncall"` → secondary, `"engineering_manager"` → synthetic RosterMember with step.phone override, else → primary; `"slack_channel"` notify type → `backends["slack_dm"].send_to_channel(slack_channel, payload)`; other notify types → `backends[step.notify].send(recipient, payload)`; missing backend logs warning and returns (never raises); appends `NotificationResult` to `state.notifications_sent`; `_run_loop`: polls with `min(wait.total_seconds(), 5.0)` sleep (floor 1.0s), logs `escalation_exhausted` warning when EXHAUSTED; `shutdown()`: cancels all active tasks via `asyncio.gather(..., return_exceptions=True)`

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

<!-- AUTO-MANAGED: safe-action-registry -->
## Safe Action Registry (`registry/safe-actions.yaml`)

Policy source of truth for all safe actions. Python enforcement logic (novel action rejection, approval ratchet, applicability checks, blast radius validation) stays in `safe_actions/registry.py`; this file declares what exists and when it applies.

**5 built-in actions:**

| Action | Risk | Approval | Cooldown | Target | Applicable to |
|--------|------|----------|----------|--------|---------------|
| `rollback` | high | required | 300s | service | api, worker, ai-gate / deployment_regression, model_regression, config_change |
| `scale_up` | low | auto | 120s | service | api, worker only — **NOT ai-gate** ("AI gate failures are judgment quality issues, not capacity") |
| `disable_feature_flag` | medium | required | 60s | feature_flag | api, worker, ai-gate / feature_regression, config_change, a_b_test_failure |
| `reduce_autonomy` | low | auto | 0s | agent | ai-gate only — **NOT api/worker** ("Autonomy reduction targets AI agents, not infrastructure services") |
| `pause_pipeline` | medium | required | 60s | service | api, worker, ai-gate / deployment_regression, cascading_failure |

**YAML schema per action:** `description`, `risk` (high/medium/low), `requires_approval` (bool), `cooldown_seconds`, `target_type`, `applicable_to.service_types[]`, `applicable_to.failure_modes[]`, optional `not_applicable_to.service_types[]` + `not_applicable_to.reason`, `blast_radius` (prose), `estimated_recovery`.

**Handler wiring:** `register_builtin_actions()` in `safe_actions/actions.py` checks each action's `binding` field at registration time: if `binding` is present and not `"stub"`, creates a `WebhookDispatcher`-backed async handler via `_make_webhook_handler(binding)`; otherwise falls back to `_HANDLERS[name]` stub. Actions with no handler and no binding emit `logger.warning` and are skipped — not a crash.

**Execution bindings (implemented — bead opensrm-9sv.3):**

`rollback` uses a full webhook binding (live). The other 4 actions use `binding: stub` (stub handlers fire). Each action in `safe-actions.yaml` can declare a `binding` section:

```yaml
binding:
  method: webhook
  url: "https://example.internal/api/{{service}}/rollback"  # {{variable}} template syntax
  headers:
    Authorization: "Bearer ${SECRET_TOKEN}"  # ${ENV_VAR} resolved at execution time
  body: { ... }
  timeout: 30
  retry:
    attempts: 3
    backoff: [1, 2, 4]
  verify_after:            # optional PromQL-based post-execution check
    wait: 60               # seconds before querying
    prometheus_url: "${PROMETHEUS_URL}"
    query: >               # must return scalar boolean (comparison operator)
      rate(http_requests_total{service="{{service}}", status=~"5.."}[2m]) < 0.01
    description: "human-readable success condition"
```

`verify_after` result semantics: query returns `1` → `verified: true`; returns `0` → `verified: false` (with detail); Prometheus unreachable or no results → `verified: null` (warning, not failure). Result stored in verdict `metadata.custom.execution`.

`safe_actions/webhook.py` — `WebhookDispatcher.execute(binding, variables)` renders templates → resolves secrets → POSTs via `httpx.AsyncClient` with retry → optionally calls `_verify`. `render_binding_templates(obj, variables)` handles `{{var}}` and `{{ var }}` forms; missing variables left as-is. `resolve_secrets(obj)` raises `ValueError` on missing env var — never logs secret values.

Template variables available in bindings: `{{service}}` / `{{target}}` (from remediation result), `{{incident_id}}`, `{{severity}}`, `{{previous_revision}}` (from correlation verdict change event). Secrets (`${ENV_VAR}`) never logged, stored in verdicts, or included in prompts — missing var = clear error at execution time.
<!-- END AUTO-MANAGED -->

<!-- AUTO-MANAGED: prompts -->
## Prompt Definitions (`prompts/`)

YAML-based prompt definitions — migration from hardcoded Python strings to versioned YAML files complete. All 4 agents load prompts from YAML via `load_prompt(_PROMPT_PATH)` at `build_prompt()` call time.

**YAML structure:** each file has `name`, `version`, `system` (with `{schema_block}` placeholder injected at load time by `nthlayer_common.prompts.load_prompt`; judgment SLO target embedded here), `response_schema` (JSON Schema), and `user_template` (`{{ context }}` — full incident context passed at call time).

**Wiring pattern** (all 4 agents):
```python
from nthlayer_common.prompts import extract_confidence, load_prompt, render_user_prompt
_PROMPT_PATH = Path(__file__).parent.parent.parent.parent / "prompts" / "triage.yaml"

def build_prompt(self, context):
    spec = load_prompt(_PROMPT_PATH)
    # ... assemble user content ...
    return spec.system, user_content
```

| File | Agent | Judgment SLO | Key schema fields |
|------|-------|--------------|-------------------|
| `prompts/triage.yaml` | `TriageAgent` | Severity reversal rate < 10% | `severity` (int 0-4), `blast_radius[]`, `affected_slos[]`, `assigned_team`, `reasoning`, `confidence` |
| `prompts/investigation.yaml` | `InvestigationAgent` | Root cause agreement 70% | `hypotheses[].{description, confidence, evidence, change_candidate}`, `root_cause`, `root_cause_confidence`, `reasoning`, `confidence`; `root_cause_threshold` variable in system prompt |
| `prompts/remediation.yaml` | `RemediationAgent` | Fix success rate 80% | `proposed_action`, `target`, `risk_assessment`, `requires_human_approval`, `reasoning`, `autonomy_reduction`, `confidence`; single `{{ safe_actions }}` variable in system prompt — formatted by `_format_safe_actions()` with risk levels, approval requirements, applicable failure modes, and `not_applicable_to` constraints loaded from `registry/safe-actions.yaml` |
| `prompts/communication.yaml` | `CommunicationAgent` | Human edit rate < 15% | `updates[].{channel, update_type, content}`, `reasoning`, `confidence` |
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
- `__init__` signature: `(model, max_tokens, verdict_store, config, timeout=None, decision_store=None)` — `decision_store` is an optional `SQLiteDecisionRecordStore`; stored as `self._decision_store`
- `_call_model`: delegates to `nthlayer_common.llm.llm_call` via `asyncio.to_thread` + `asyncio.wait_for` timeout; model format `"provider/model"` (anthropic, openai, ollama, etc.)
- `_emit_verdict`: sets `subject.type=role.value`, wires `lineage.context` from `context.trigger_verdict_ids`, chains `lineage.parent` from `context.verdict_chain[-1]`; confidence fallback is `0.0` (not `0.5`) — `getattr(result, "root_cause_confidence", None) or getattr(result, "confidence", None) or 0.0`
- `_write_decision_verdict(context, verdict, system_prompt, user_prompt, response_text)`: no-op if `self._decision_store is None`; uses `nthlayer_common.records.verdict_bridge.build_decision_verdict` to produce a content-addressed Verdict record (agent=self.role.value, model=self._model; action={"action": verdict.judgment.action, "subject_type": verdict.subject.type}; reads chain tail via `get_chain("verdict", self.role.value)`; stores via `put_verdict` + `put_prompt` + `put_response`); fail-open (logs `decision_verdict_write_failed` on error)
- `_build_service_context_prompt`: builds a service context section for agent prompts from `context.metadata.service_context`; emits service name, service_type (labelled "AI decision service" or "traditional service"), tier, team, breached SLO name/type with description ("JUDGMENT SLO — measures decision quality" vs "measures infrastructure reliability"), current/target values, declared SLOs list, and role-specific remediation guidance (AI gate: model rollback/canary revert/autonomy reduction; infra: rollback/scale_up/restart/feature flag disable); returns `""` if no `service_context` key present
- `_prune_topology(topology, relevant_services)`: prunes topology dict to `relevant_services` + 1 hop of forward dependencies (from each service's `dependencies` list); returns topology unchanged if either arg is empty; used by all concrete agents in `build_prompt()` to reduce prompt token cost — triage prunes to `trigger_service` from `context.metadata`, investigation/remediation prune to `context.triage.blast_radius`; fall back to full topology if the relevant set is empty
- `_degraded_verdict`: emits `confidence=0.0`, `action="escalate"`, tags `["degraded","human-takeover-required"]` when model fails; delegates subject summary to `_build_degraded_summary`
- `_build_degraded_summary`: constructs informative degraded subject summary from `context.metadata` — extracts `blast_radius`, `root_causes[0].service/type`, `severity`, `incident_id`; role-specific format: triage → `"DEGRADED: SEV-N — service type, K services in blast radius"`; investigation → `"DEGRADED: Manual investigation required — root cause from correlation: service (type)"`; communication → `"DEGRADED: Draft status update required for {incident_id}"`; remediation → `"DEGRADED: Manual remediation required — see correlation verdict for recommended actions"`
- `_parse_json`: strips markdown fences and preamble before parsing; handles `{...}` extraction from noisy model output via brace-depth matching
- `_build_summary`: role-specific subject summary for emitted verdicts — triage: `"SEV-{sev}: {first sentence of reasoning}"` or fallback `"SEV-{sev} — {N} services in blast radius[, assigned to {team}]"`; investigation: `"Root cause ({confidence:.0%} confidence): {rc[:90]}"` or `"Hypothesis: {desc[:90]}"` or first sentence of reasoning ([:90]) or `"Agent response produced no summary — see raw output"` (with `log.warning`); communication: `"{'via ' + channel + ': ' if channel else ''}{content[:90]}"` (channel omitted if empty) or first sentence of reasoning ([:90]) or generic `"Agent response produced no summary — see raw output"` (no communication-specific context fallback); remediation: `"{action} on {target}[ (requires approval)]"` (approval suffix only when `requires_human_approval=True`; no auto-approved text; no risk suffix) or `"Proposed: {action}"` (no approval suffix on action-only branch) or first sentence of reasoning ([:90]) or context-based fallback from `metadata.root_causes`
- `execute()` template method: calls `build_prompt` → `_call_model` → `parse_response` → `_apply_result` → `_build_summary` → `_emit_verdict(action="flag")` → `_write_decision_verdict(context, verdict, system, user, response)` → `_notify_slack(context)` → `_post_execute`; on any exception logs `agent_execute_failed` at ERROR with `exc_info=True` before emitting `_degraded_verdict`
- `_notify_slack(context: IncidentContext) -> None`: called after each successful verdict emission; checks `SLACK_WEBHOOK_URL` env var (skips if absent); selects block_builder by agent role (triage→`build_triage_blocks`, remediation→`build_remediation_blocks`, resolution→`build_resolution_blocks`); calls `send_slack_notification(verdict, block_builder, verdict_store, trigger_verdict_ids=context.trigger_verdict_ids)`; fail-open — Slack errors never propagate to the incident pipeline
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

**Execution binding abstraction (implemented):**
- Binding is at the handler level — `SafeAction.execute()` in `registry.py` is unchanged; `actions.py` wraps `WebhookDispatcher.execute()` into a handler callable when `binding` is present and not `"stub"`
- Existing approval ratchet, cooldown, and blast radius checks are all pre-handler and remain unmodified
- `_make_webhook_handler(binding_config)` in `actions.py` creates the async handler closure; lazily imports `WebhookDispatcher`; uses `_build_variables(target, context, kwargs)` to assemble template variable dict (`service`, `target`, `incident_id`; `severity` if `context.triage` present; any string kwargs)
- `rollback` binding active: ArgoCD webhook, `${ARGOCD_TOKEN}` secret, retry 3x with backoff [1,2,4]s, PromQL verify after 60s
- Validation warnings (at startup or `nthlayer validate`): action declared with no binding and not `binding: stub`; `${ENV_VAR}` not set; `{{variable}}` in `verify_after.query` not in available variable list

**Slack Notifications (`notifications.py`):**

`src/nthlayer_respond/notifications.py` — block builders for incident lifecycle messages and shared transport utilities.

Block builders (all return `tuple[list[dict], str]` — blocks + fallback text):
- `build_triage_blocks(verdict, context=None)` — INCIDENT OPENED; first sentence of `subject.summary`; footer with "nthlayer-respond · confidence X.XX · {id}"
- `build_remediation_blocks(verdict, context=None)` — REMEDIATION; first sentence of summary
- `build_approval_blocks(verdict, incident_id, context=None)` — APPROVAL REQUIRED; approve/reject action buttons; `block_id=f"approval_{incident_id}"`; button `action_id` values are `"approve"` / `"reject"`, `value` is `incident_id`
- `build_verification_blocks(verdict, verified: bool | None)` — VERIFIED / VERIFICATION FAILED / VERIFICATION UNKNOWN; `subject.summary[:200]`
- `build_resolution_blocks(verdict, context=None)` — INCIDENT RESOLVED; full chain text "evaluate → correlate → triage → investigate → remediate → learn"

Transport utilities:
- `find_slack_thread_ts(verdict_store, verdict_ids) -> str | None` — walks `lineage.context` one hop up; returns first `slack_thread_ts` found or `None`
- `send_slack_notification(verdict, block_builder, verdict_store=None, trigger_verdict_ids=None, **builder_kwargs) -> None` — async helper; reads `SLACK_WEBHOOK_URL` from env; calls `find_slack_thread_ts` for threading; calls `block_builder(verdict, **kwargs)`; sends via `SlackNotifier`; if new thread started and API returns `ts`, stores `slack_thread_ts` in `verdict.metadata.custom` and calls `verdict_store.put(verdict)`
- `resolve_slack_channel(context, env_fallback=None) -> str | None` — resolution order: (1) `spec.ownership.slack_channel` from manifest's `service_context`; (2) `SLACK_CHANNEL_ID` env var or `env_fallback` arg; (3) `None`

Activation: `SLACK_WEBHOOK_URL` env var — absent = zero impact on incident pipeline (fully opt-in).

**ApprovalServer (`server.py`) HTTP routes and Slack interaction flow:**
- `POST /api/v1/incidents/{id}/approve` — `approved_by` from optional JSON body; 400 on malformed JSON (`{"error": "Invalid JSON body"}`); 404 if incident not found; 409 on wrong state; 200 with state/action/target/approved_by/execution_result/verdict_id on success
- `POST /api/v1/incidents/{id}/reject` — `reason` required (400 if missing); `rejected_by` optional; 400 on malformed JSON; 404 if not found; 409 on wrong state
- `GET /api/v1/incidents/{id}` — returns incident state, trigger_source, proposed_action, target, requires_human_approval, executed, severity
- `POST /api/v1/slack/interactions` — verifies signature via `SlackWebClient.verify_signature` (returns 401 on failure, 403 if `slack_signing_secret` not configured); parses form-encoded payload; routes `action_id == "approve"` / `"reject"` to coordinator; fires `_update_slack_message` as background task
- `_update_slack_message`: calls `SlackWebClient.update_message` to replace buttons with "✅ Approved by @user" or "❌ Rejected by @user" confirmation text
- Per-incident `asyncio.Lock` serializes concurrent approve/reject/timeout operations
- Approval timeout: `start_timeout(incident_id)` starts a background `asyncio.Task`; auto-rejects with reason `"Approval timed out after {N}s"` and `rejected_by="system/timeout"` if still `AWAITING_APPROVAL` after `approval_timeout_seconds`; `recover_pending_approvals()` called on server startup to resume timeouts for any pre-existing `AWAITING_APPROVAL` incidents with remaining time; incidents that expired during downtime are immediately rejected
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
- `approve(incident_id, approved_by=None)`: executes the safe action, emits `"remediation"` verdict with `action="approve"`, `confidence=1.0`, `reasoning="{who} approved {action} on {target}"`; stores `approved_by` in `verdict.metadata.custom["approved_by"]`; state → RESOLVED. On execution failure: emits `action="escalate"`, `confidence=0.0`; state → ESCALATED.
- `reject(incident_id, reason, rejected_by=None)`: resolves the last verdict as `"overridden"` with `override={"by": who, "reasoning": "{who} rejected {action} of {target}: {reason}"}` where `who = rejected_by or "human"`; `rejected_by` is NOT stored in `verdict.metadata.custom` — identity lives only in `override.by`; on `verdict_store.resolve` failure logs warning but still sets state → ESCALATED.

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
