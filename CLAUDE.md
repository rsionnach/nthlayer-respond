# Mayday

<!-- AUTO-MANAGED: project-description -->
## Project Description

Multi-agent incident response system coordinated by AI. Agents collaborate to triage, investigate, communicate, and remediate incidents under human supervision. Mayday owns the incident lifecycle; PagerDuty, Slack, and email are notification channels it uses, not upstream incident sources.

- Status: architecture phase only — implementation begins in Phase 3 (after Phases 0-2 complete)
- Beads epic: mayday-bel
- Depends on: sitrep-5yh completion (Phase 2 SitRep implementation)
- Demo milestone: Demo 3 "The Full Chain" — SitRep correlation verdicts feed Mayday pipeline, verdict lineage chain complete
- Accept criteria: `mayday replay --scenario scenarios/synthetic/cascading-failure.yaml` produces expected verdicts with full lineage
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

**Crash recovery:**
- `IncidentContext` serialised to SQLite after each agent step
- Coordinator resumes from last persisted step on restart
- Testable with mock agents (no model required)

**Post-incident learning loop:**
- Manifest updates (tighter SLO targets, new dependency declarations, new safe action definitions)
- NthLayer alerting rule refinements from quality patterns
- Arbiter judgment SLO threshold revisions from historical data
- SitRep correlation improvements from past incident accuracy

**Implementation phases (Phase 3):**
- 3.1: Coordinator + crash recovery (state machine, IncidentContext persistence, mock-agent testable)
- 3.2: Agent base class + Triage Agent (verdict emission with lineage)
- 3.3: Investigation Agent (hypothesis generation, `subject.type="investigation"`)
- 3.4: Safe Action Registry + Remediation Agent (closed condition registry)
- 3.5: Communication Agent + human input + post-incident verdict resolution
<!-- END AUTO-MANAGED -->

<!-- AUTO-MANAGED: verdict-integration -->
## Verdict Integration

Mayday is designed to produce verdicts for every agent judgment and consume SitRep's correlation verdicts as input (see `verdicts/` and `VERDICT-INTEGRATION.md`). Integration is planned but not yet implemented.

**Install:** `pip install -e ../verdicts` (path-based dependency; verdict API is frozen after Phase 0)

**Shared store:** single `verdicts.db` with WAL mode — NOT a per-component store. All ecosystem components read/write the same file.

**Consuming SitRep verdicts (planned):**
```python
sitrep_verdicts = verdict_store.query(VerdictFilter(
    producer_system="sitrep", subject_type="correlation",
    from_time=last_30_minutes, limit=0,
))
# verdicts provide: timestamp (staleness), confidence (trust level), lineage (provenance)
```

**Producing verdicts per agent role (planned):**
- Triage Agent → `subject.type: "triage"` verdict (severity, blast radius)
- Investigation Agent → `subject.type: "investigation"` verdict (hypotheses, root cause)
- Communication Agent → `subject.type: "communication"` verdict (status update content) — requires `"communication"` added to `VALID_SUBJECT_TYPES` in Phase 0.2
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
