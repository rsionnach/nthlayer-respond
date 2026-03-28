# src/nthlayer_respond/agents/investigation.py
"""InvestigationAgent — hypothesis generation and root cause declaration."""
from __future__ import annotations

import json

from nthlayer_respond.agents.base import AgentBase
from nthlayer_respond.types import (
    AgentRole,
    Hypothesis,
    IncidentContext,
    InvestigationResult,
)


class InvestigationAgent(AgentBase):
    """Form hypotheses about root cause and rank by confidence.

    Judgment SLO: 70% post-incident agreement on declared root causes.
    Root cause is only declared when confidence exceeds root_cause_threshold.
    """

    role = AgentRole.INVESTIGATION
    default_timeout = 60

    # ------------------------------------------------------------------ #
    # Judgment interface                                                   #
    # ------------------------------------------------------------------ #

    def build_prompt(self, context: IncidentContext) -> tuple[str, str]:
        threshold = self._config.get("root_cause_threshold", 0.7)

        system = (
            "You are an investigation agent. "
            "Form hypotheses about root cause, rank by confidence. "
            "Judgment SLO: 70% post-incident agreement. "
            f"Only declare root_cause if your confidence exceeds {threshold}. "
            "Otherwise leave root_cause null and list hypotheses. "
            "Respond with ONLY valid JSON."
        )

        parts: list[str] = []

        # Triage context
        if context.triage is not None:
            t = context.triage
            parts.append("Triage results:")
            parts.append(f"  severity={t.severity}")
            parts.append(f"  blast_radius={t.blast_radius}")
            parts.append(f"  affected_slos={t.affected_slos}")

        # nthlayer-correlate correlation verdicts
        if context.trigger_verdict_ids:
            parts.append("\nnthlayer-correlate correlation verdicts:")
            for vid in context.trigger_verdict_ids:
                try:
                    v = self._verdict_store.get(vid)
                    if v is not None:
                        parts.append(
                            f"  - ref={v.subject.ref!r}  "
                            f"summary={v.subject.summary!r}  "
                            f"confidence={v.judgment.confidence}  "
                            f"reasoning={v.judgment.reasoning!r}"
                        )
                except Exception:  # noqa: BLE001
                    pass

        # Service context from OpenSRM spec + evaluation verdict
        svc_ctx = self._build_service_context_prompt(context)
        if svc_ctx:
            parts.append(svc_ctx)

        # Topology
        parts.append(f"\nTopology: {json.dumps(context.topology)}")

        user = "\n".join(parts)
        return system, user

    def parse_response(
        self, response: str, context: IncidentContext
    ) -> InvestigationResult:
        data = self._parse_json(response)
        threshold = self._config.get("root_cause_threshold", 0.7)

        # Build hypotheses list — accept both "description" and "hypothesis" field names
        hypotheses: list[Hypothesis] = []
        for h in data.get("hypotheses") or []:
            desc = h.get("description") or h.get("hypothesis") or h.get("summary", "")
            raw_evidence = h.get("evidence") or []
            if not raw_evidence and h.get("reasoning"):
                raw_evidence = [h["reasoning"]]
            hypotheses.append(
                Hypothesis(
                    description=desc,
                    confidence=float(h.get("confidence", 0.0)),
                    evidence=list(raw_evidence),
                    change_candidate=h.get("change_candidate"),
                )
            )

        root_cause: str | None = data.get("root_cause")
        root_cause_confidence: float = float(data.get("root_cause_confidence") or data.get("confidence") or 0.0)
        reasoning: str = data.get("reasoning", "") or data.get("analysis", "")

        # Mechanical threshold check: clear root_cause if confidence is below threshold
        if root_cause is not None and root_cause_confidence < threshold:
            root_cause = None

        return InvestigationResult(
            hypotheses=hypotheses,
            root_cause=root_cause,
            root_cause_confidence=root_cause_confidence,
            reasoning=reasoning,
        )

    def _apply_result(
        self, context: IncidentContext, result: InvestigationResult
    ) -> IncidentContext:
        context.investigation = result
        return context
