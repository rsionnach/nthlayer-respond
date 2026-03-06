# Mayday

<!-- AUTO-MANAGED: project-description -->
## Project Description

Multi-agent incident response system coordinated by AI. Agents collaborate to triage, investigate, communicate, and remediate incidents under human supervision. Mayday owns the incident lifecycle; PagerDuty, Slack, and email are notification channels it uses, not upstream incident sources.

- Status: architecture phase only — implementation has not started
- License: Apache 2.0
- Contributing: see CONTRIBUTING.md
<!-- END AUTO-MANAGED -->

<!-- AUTO-MANAGED: architecture -->
## Architecture

Follows [Zero Framework Cognition](ZFC.md): the orchestrator is pure transport; agents provide judgment.

**Orchestrator** (deterministic state machine, not an agent):
- Receives incident trigger, creates shared context, sequences agent pipeline, routes messages
- Degrades to "no AI opinion" (not "no incident response") when model is unavailable

**Agent pipeline** (sequencing):
1. Triage (first)
2. Investigation + Communication (parallel)
3. Remediation + Communication update (after root cause found)

**Shared Incident Context** — single accumulating YAML object all agents read/write:
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

**Human-in-the-loop:**
- Agents never take destructive action without human approval unless the action is pre-approved as safe in the OpenSRM manifest
- Every agent decision emits OTel telemetry
- Every human override feeds back into that agent's judgment SLO
- Arbiter uses a one-way safety ratchet: can reduce agent autonomy but cannot increase it without human approval

**Post-incident learning loop:**
- Manifest updates (tighter SLO targets, new dependency declarations, new safe action definitions)
- NthLayer alerting rule refinements from quality patterns
- Arbiter judgment SLO threshold revisions from historical data
- SitRep correlation improvements from past incident accuracy
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
- [Arbiter](https://github.com/rsionnach/arbiter) — quality measurement and AI agent governance
- [NthLayer](https://github.com/rsionnach/nthlayer) — generate monitoring infrastructure from manifests
- [SitRep](https://github.com/rsionnach/sitrep) — situational awareness through signal correlation
- Mayday (this repo) — multi-agent incident response
<!-- END AUTO-MANAGED -->
