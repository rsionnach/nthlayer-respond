# Mayday

**Multi-agent incident response coordinated by AI.**

[![Status: Architecture](https://img.shields.io/badge/Status-Architecture-blue?style=for-the-badge)](https://github.com/rsionnach/mayday)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-green?style=for-the-badge)](LICENSE)

Incident response involves a lot of repetitive work: classifying severity, identifying blast radius, correlating changes with symptoms, drafting stakeholder updates, deciding whether to rollback, and communicating resolution. Most of this work follows patterns that AI agents can handle reliably, freeing human responders for the judgment calls that actually need them (novel failure modes, business-critical tradeoffs, cross-team coordination).

Mayday is an incident response system where specialised AI agents collaborate to triage, investigate, communicate, and remediate under human supervision. Each agent has a clear domain, defined decision authority, and its own judgment SLO that measures how often humans need to correct its work. Mayday owns the incident lifecycle, and uses tools like PagerDuty, Slack, and email as notification channels rather than treating them as upstream incident sources.

This project is in the architecture phase. The design is documented below, and implementation has not yet started.

---

## Alert Flow

```
Alert Source (Arbiter quality breach / Prometheus alert / any webhook)
       │
       ▼
   SitRep Snapshot (correlated context)
       │
       ▼
   Mayday Orchestrator (creates incident context)
       │
       ▼
   Agent Pipeline (triage → investigate + communicate → remediate)
       │
       ▼
   Notification Channels (PagerDuty / Slack / email / status page)
```

Mayday receives alerts from any source (the Arbiter's quality breach signals, Prometheus alerting rules, or any webhook), requests a correlated snapshot from SitRep for context, then coordinates the response through its agent pipeline. PagerDuty, Slack, email, and status pages are notification channels that Mayday uses to reach humans when it needs approval or escalation.

---

## Orchestration Model

Mayday uses a purpose-built orchestrator (not a general-purpose agent framework) that sequences agents based on the incident lifecycle. The orchestrator itself is not an agent. It's a deterministic state machine that sequences agent execution (transport). Agents reason within their step (judgment). This follows [Zero Framework Cognition](ZFC.md).

```
┌──────────────┐
│    Triage    │  severity, blast radius, initial assignment
└──────┬───────┘
       │
       ├───────────────────────┐
       ▼                       ▼
┌──────────────┐       ┌──────────────┐
│Investigation │       │Communication │  initial stakeholder notification
└──────┬───────┘       └──────┬───────┘
       │                       │
       │ root cause found      │
       ├───────────────────────┤
       ▼                       ▼
┌──────────────┐       ┌──────────────┐
│ Remediation  │       │Communication │  update with root cause + fix
└──────────────┘       └──────────────┘
```

Triage runs first, then Investigation and Communication run in parallel. When Investigation produces a root cause, Remediation begins and Communication sends an update with the findings and fix.

---

## Incident Context

All Mayday agents read from and write to a shared incident context object that accumulates findings throughout the incident lifecycle. This is the single accumulating record of what is known about the incident:

```yaml
incident_context:
  id: INC-2026-0142
  declared_at: "2026-02-23T14:32:00Z"
  source: arbiter_quality_breach

  triage:
    severity: P1
    blast_radius: [checkout-service, payment-gateway]
    affected_slos: [checkout-availability, payment-latency-p99]
    assigned_teams: [platform-checkout, platform-payments]

  investigation:
    hypotheses:
      - id: H1
        description: "model version update at 14:28 introduced quality regression"
        confidence: 0.82
        evidence: [sitrep-correlation-id-847, arbiter-quality-drop]
      - id: H2
        description: "database connection pool exhaustion"
        confidence: 0.34
        evidence: [log-pattern-conn-timeout]
    root_cause: H1

  communication:
    updates_sent:
      - channel: "#platform-incidents"
        timestamp: "2026-02-23T14:33:12Z"
        type: initial_notification
      - channel: status_page
        timestamp: "2026-02-23T14:35:00Z"
        type: investigating

  remediation:
    proposed_action: rollback_model_version
    target: rig-webapp
    risk_assessment: low
    requires_human_approval: false
    executed_at: null
```

---

## Agent Roles

Each agent has a defined domain, specific decision authority, and its own judgment SLO that measures how reliably it performs its role.

### Triage Agent

Classifies severity based on blast radius and SLO impact from OpenSRM manifests. Determines which services are affected, which teams own them, and how urgent the response needs to be.

**Can:** Set severity, notify teams (via PagerDuty/Slack as notification channels), assign ownership.
**Cannot:** Remediate. Override existing classification without human approval.
**Judgment SLO:** Reversal rate on severity classifications (target less than 10%).

### Investigation Agent

Generates hypotheses from SitRep snapshots, gathers evidence from metrics, logs, and change history, and ranks root causes by confidence. Adapts investigation strategy based on what evidence reveals, following the data rather than a fixed checklist.

**Can:** Form and rank hypotheses. Declare root cause when confidence exceeds threshold.
**Cannot:** Execute any remediation.
**Judgment SLO:** Root cause agreement with post-incident review (target 70% at maturity).

### Communication Agent

Produces audience-appropriate messaging via appropriate channels. Selects communication channels based on severity and stakeholder type, and decides timing (too frequent is noise, too infrequent loses trust).

**Can:** Draft and send updates within pre-approved templates. Choose channels and timing.
**Cannot:** Contradict investigation findings. Communicate resolution until confirmed.
**Judgment SLO:** Human edit rate on outgoing communications (target less than 15%).

### Remediation Agent

Selects and executes fixes based on investigation findings, manifest-defined safe actions, and risk assessment.

**Can:** Suggest fixes to humans. Execute pre-approved safe actions (rollback, scale up, disable feature flag) without human approval.
**Cannot:** Execute novel remediation not pre-approved in the OpenSRM manifest. Make changes to services outside the blast radius.
**Judgment SLO:** Fix success rate (target 80%).

---

## Human-in-the-Loop Design

Agents never take destructive action without human approval unless the action is pre-approved as safe in the OpenSRM manifest. The manifest defines which actions are considered safe for automated execution (like rolling back to a known-good version or scaling up), and everything else requires a human to approve.

Humans make severity calls, approve novel remediations, and override agent decisions. Every agent decision emits OTel telemetry, and every human override feeds back into that agent's judgment SLO. The [Arbiter's](https://github.com/rsionnach/arbiter) governance layer monitors these judgment SLOs and adjusts agent autonomy accordingly, using the one-way safety ratchet (the Arbiter can reduce agent autonomy but cannot increase it without human approval).

---

## Post-Incident Learning

After resolution, Mayday produces structured findings that flow back into the ecosystem rather than sitting in a document that nobody reads again:

- **Manifest updates:** Findings map to specific OpenSRM manifest changes (tighter SLO targets that were too loose, new dependency declarations that were missing, new safe action definitions for remediation)
- **Rule refinements:** Quality patterns inform NthLayer's generated alerting rules (alerts that should have fired earlier or didn't fire at all)
- **Threshold revisions:** The Arbiter's historical data informs whether judgment SLO thresholds need adjustment
- **Correlation improvements:** SitRep's accuracy on past incidents calibrates its future correlations

This closes the learning loop so the system improves after every incident rather than just documenting what happened.

---

## OpenSRM Integration

Mayday reads from [OpenSRM](https://github.com/rsionnach/opensrm) manifests extensively during incident response:

- **Severity tiers** and SLO targets determine how urgently a degradation should be treated
- **Safe action definitions** in the manifest specify which remediation actions the Remediation Agent can execute without human approval
- **Dependency topology** tells the Investigation Agent which services to check when a dependency is affected
- **Escalation paths** and ownership metadata tell the Communication Agent which teams to notify and how to reach them

---

## Ecosystem Integration

Mayday consumes from and produces signals for the other ecosystem components:

- **SitRep** provides correlated snapshots as the starting context for every incident, so Mayday's agents begin with a correlated picture rather than raw signals
- **The Arbiter** provides quality scores that inform whether AI agents in the response are producing reliable diagnostics, and its governance layer adjusts Mayday's agent autonomy based on measured performance
- **NthLayer** provides topology exports and deployment gate status, and consumes Mayday's post-incident findings to refine generated alerting rules

---

## OpenSRM Ecosystem

Mayday is one component in the OpenSRM ecosystem. Each component solves a complete problem independently, and they compose when used together through shared OpenSRM manifests and OTel telemetry conventions.

```
                        ┌─────────────────────────┐
                        │     OpenSRM Manifest     │
                        │  (the shared contract)   │
                        └────────────┬────────────┘
                                     │
                    reads            │           reads
               ┌─────────────┬──────┴──────┬─────────────┐
               ▼             ▼             ▼             ▼
         ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐
         │ Arbiter  │ │ NthLayer │ │  SitRep  │ │>>MAYDAY< │
         │          │ │          │ │          │ │          │
         │ quality  │ │ generate │ │correlate │ │ incident │
         │+govern   │ │ monitoring│ │ signals  │ │ response │
         │+cost     │ │          │ │          │ │          │
         └────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘
              │             │             │             │
              └─────────────┴──────┬──────┴─────────────┘
                                   ▼
                     ┌──────────────────────────┐
                     │      Verdict Store       │
                     │  (shared data substrate) │
                     │ create · resolve · link  │
                     │ accuracy · gaming-check  │
                     └────────────┬─────────────┘
                                  │ OTel side-effects
                                  ▼
                     ┌──────────────────────────┐
                     │    OTel Collector /      │
                     │   Prometheus / Grafana   │
                     └──────────────────────────┘

              Learning loop (post-incident):
              Mayday findings → manifest updates
              → NthLayer regenerates → Arbiter
              refines → SitRep improves → OpenSRM
```

**How Mayday fits in:**

- Mayday consumes **SitRep's correlation verdicts** (with confidence scores and lineage) as starting context for every incident, so agents begin with a correlated picture rather than raw signals
- Each Mayday agent emits its own **verdicts** (triage, investigation, communication, remediation) linked via lineage to the SitRep verdicts that informed them — one human override at any point calibrates every component upstream
- The **Arbiter** monitors Mayday's agent judgment SLOs and adjusts autonomy via the one-way safety ratchet
- **NthLayer** provides topology exports and deployment gate status, and consumes post-incident findings as rule refinements

Each component works alone. Someone who just needs incident response coordination adopts Mayday without needing NthLayer, the Arbiter, or SitRep (though SitRep's correlated verdicts significantly enrich Mayday's context).

| Component | What it does | Link |
|-----------|-------------|------|
| **OpenSRM** | Specification for declaring service reliability requirements | [opensrm](https://github.com/rsionnach/opensrm) |
| **Verdict** | Data primitive for recording AI judgments and measuring correctness | [verdicts](https://github.com/rsionnach/verdicts) |
| **Arbiter** | Quality measurement and governance for AI agents | [arbiter](https://github.com/rsionnach/arbiter) |
| **NthLayer** | Generate monitoring infrastructure from manifests | [nthlayer](https://github.com/rsionnach/nthlayer) |
| **SitRep** | Situational awareness through signal correlation | [sitrep](https://github.com/rsionnach/sitrep) |
| **Mayday** | Multi-agent incident response (this repo) | [mayday](https://github.com/rsionnach/mayday) |

---

## Architecture

Mayday follows [Zero Framework Cognition](ZFC.md). The orchestrator is pure transport: it receives the incident trigger, creates the shared context, sequences agent execution through the pipeline, and routes messages. The agents provide judgment: triaging severity, forming hypotheses, drafting communications, and assessing remediation risk. If the model is unavailable, the orchestrator still creates the incident context and routes it to human operators, degrading to "no AI opinion" rather than "no incident response."

---

## Status

Mayday is in the architecture phase. The design documented here reflects the target architecture, and implementation has not yet started. The orchestration model and agent role definitions have been designed in detail (see the [Mayday architecture](https://github.com/rsionnach/opensrm/blob/main/components/mayday/README.md) in the OpenSRM repo).

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

---

## License

Apache License 2.0. See [LICENSE](LICENSE).
