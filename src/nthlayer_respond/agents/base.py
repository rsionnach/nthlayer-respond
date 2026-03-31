# src/nthlayer_respond/agents/base.py
"""AgentBase ABC — ZFC boundary: transport here, judgment in subclasses."""
from __future__ import annotations

import asyncio
import json
import re
import urllib.request
import urllib.error
from abc import ABC, abstractmethod
from typing import Any

import structlog

log = structlog.get_logger(__name__)

from nthlayer_learn import create as verdict_create, Verdict

from nthlayer_respond.types import AgentRole, IncidentContext


class AgentBase(ABC):
    """Abstract base class for all Mayday agents.

    Transport concerns (model calls, verdict emission, HTTP governance
    requests) live here.  Judgment concerns (prompt construction, response
    parsing, result application) are abstract and implemented by subclasses.
    """

    # Subclasses MUST declare these class-level attributes.
    role: AgentRole
    default_timeout: int = 30

    # ------------------------------------------------------------------ #
    # Construction                                                         #
    # ------------------------------------------------------------------ #

    def __init__(
        self,
        model: str,
        max_tokens: int,
        verdict_store: Any,
        config: dict,
        timeout: int | None = None,
    ) -> None:
        self._model = model
        self._max_tokens = max_tokens
        self._verdict_store = verdict_store
        self._config = config
        self._timeout = timeout if timeout is not None else self.default_timeout

    # ------------------------------------------------------------------ #
    # Transport: model                                                     #
    # ------------------------------------------------------------------ #

    async def _call_model(self, system_prompt: str, user_prompt: str) -> str:
        """Call the LLM via the shared nthlayer-common wrapper.

        Uses asyncio.to_thread for the sync httpx call, wrapped in
        asyncio.wait_for for timeout enforcement.
        """
        from nthlayer_common.llm import llm_call

        result = await asyncio.wait_for(
            asyncio.to_thread(
                llm_call,
                system=system_prompt,
                user=user_prompt,
                model=self._model,
                max_tokens=self._max_tokens,
                timeout=self._timeout,
            ),
            timeout=self._timeout,
        )
        return result.text

    # ------------------------------------------------------------------ #
    # Transport: verdict emission                                          #
    # ------------------------------------------------------------------ #

    def _emit_verdict(
        self,
        context: IncidentContext,
        subject_summary: str,
        action: str,
        confidence: float,
        reasoning: str,
        tags: list[str] | None = None,
        dimensions: dict | None = None,
    ) -> Verdict:
        """Create, wire lineage, persist, and register a verdict."""
        judgment: dict[str, Any] = {
            "action": action,
            "confidence": confidence,
            "reasoning": reasoning,
        }
        if tags is not None:
            judgment["tags"] = tags
        if dimensions is not None:
            judgment["dimensions"] = dimensions

        v = verdict_create(
            subject={
                "type": self.role.value,
                "ref": context.id,
                "summary": subject_summary,
            },
            judgment=judgment,
            producer={"system": "nthlayer-respond", "model": self._model},
        )

        # Wire lineage
        v.lineage.context = list(context.trigger_verdict_ids)
        v.lineage.parent = context.verdict_chain[-1] if context.verdict_chain else None

        self._verdict_store.put(v)
        context.verdict_chain.append(v.id)
        return v

    def _build_service_context_prompt(self, context: IncidentContext) -> str:
        """Build a service context section for agent prompts from OpenSRM spec
        and evaluation verdict data. This gives agents the domain context they
        need to reason correctly about AI vs infrastructure failures."""
        meta = getattr(context, "metadata", {}) or {}
        svc_ctx = meta.get("service_context", {})
        if not svc_ctx:
            return ""

        lines = ["\nService context:"]
        service = svc_ctx.get("service", "unknown")
        svc_type = svc_ctx.get("service_type", "unknown")
        is_ai = svc_ctx.get("is_ai_gate", False)
        spec = svc_ctx.get("spec", {})
        ev = svc_ctx.get("evaluation", {})

        type_desc = "AI decision service" if is_ai else "traditional service"
        lines.append(f"- Service: {service}")
        lines.append(f"- Type: {svc_type} ({type_desc})")

        if spec.get("tier"):
            lines.append(f"- Tier: {spec['tier']}")
        if spec.get("team"):
            lines.append(f"- Team: {spec['team']}")

        # SLO breach details from evaluation verdict
        if ev.get("slo_name"):
            slo_name = ev["slo_name"]
            slo_type = ev.get("slo_type", "unknown")
            target = ev.get("target")
            current = ev.get("current_value")
            slo_desc = "measures decision quality, not infrastructure health" if slo_type == "judgment" else "measures infrastructure reliability"
            lines.append(f"- Breached SLO: {slo_name} ({slo_type.upper()} SLO — {slo_desc})")
            if current is not None and target is not None:
                lines.append(f"- Current value: {current} (target: < {target})")

        # SLO definitions from spec
        slos = spec.get("slos", {})
        if slos:
            lines.append(f"- Declared SLOs: {', '.join(slos.keys())}")

        # Remediation guidance based on service type
        if is_ai:
            lines.append("- This is NOT an infrastructure issue. This is an AI model quality issue.")
            lines.append("- Appropriate remediation: model rollback, canary revert, autonomy reduction")
            lines.append("- Inappropriate remediation: scale_up, restart, increase resources")
        else:
            lines.append("- This is an infrastructure/availability issue.")
            lines.append("- Appropriate remediation: rollback, scale_up, restart, feature flag disable")

        return "\n".join(lines)

    def _build_summary(self, context: IncidentContext, result) -> str:
        """Build summary strictly from the agent's actual LLM output.

        Every field must be traceable to the agent's response.
        No template-generated content that impersonates agent reasoning.
        """
        role = self.role.value
        reasoning = getattr(result, "reasoning", None) or ""

        if role == "triage":
            sev = getattr(result, "severity", None)
            blast = getattr(result, "blast_radius", None) or []
            team = getattr(result, "assigned_team", None)
            first_sentence = reasoning.split(".")[0].strip() if reasoning else ""
            if first_sentence:
                return f"SEV-{sev}: {first_sentence}"
            if sev is not None:
                parts = [f"SEV-{sev}"]
                if blast:
                    parts.append(f"{len(blast)} services in blast radius")
                if team:
                    parts.append(f"assigned to {team}")
                return " — ".join(parts)

        elif role == "investigation":
            rc = getattr(result, "root_cause", None)
            rc_conf = getattr(result, "root_cause_confidence", 0)
            if rc:
                return f"Root cause ({rc_conf:.0%} confidence): {rc[:90]}"
            hypotheses = getattr(result, "hypotheses", None) or []
            if hypotheses:
                h = hypotheses[0]
                desc = getattr(h, "description", str(h)) if hasattr(h, "description") else str(h)
                return f"Hypothesis: {desc[:90]}"

        elif role == "communication":
            updates = getattr(result, "updates_sent", None) or []
            if updates:
                u = updates[0]
                content = getattr(u, "content", "") if hasattr(u, "content") else str(u)
                channel = getattr(u, "channel", "") if hasattr(u, "channel") else ""
                return f"{'via ' + channel + ': ' if channel else ''}{content[:90]}"

        elif role == "remediation":
            action = getattr(result, "proposed_action", None)
            target = getattr(result, "target", None)
            if action and target:
                approval = getattr(result, "requires_human_approval", True)
                return f"{action} on {target}" + (" (requires approval)" if approval else "")
            if action:
                return f"Proposed: {action}"

        # If we got here, the agent-specific fields were empty.
        # Use first sentence of reasoning as last resort from actual output.
        if reasoning:
            return reasoning.split(".")[0].strip()[:90]

        # Genuinely empty — mark as unparseable
        log.warning("agent_summary_empty", role=role, incident=context.id)
        return f"Agent response produced no summary — see raw output"

    def _degraded_verdict(self, context: IncidentContext, reason: str) -> Verdict:
        """Emit a degraded escalation verdict when the agent cannot produce a
        normal judgment (e.g. model timeout, API error)."""
        summary = self._build_degraded_summary(context)
        return self._emit_verdict(
            context,
            subject_summary=summary,
            action="escalate",
            confidence=0.0,
            reasoning=f"Agent operating in degraded mode: {reason}",
            tags=["degraded", "human-takeover-required"],
        )

    def _build_degraded_summary(self, context: IncidentContext) -> str:
        """Build an informative degraded summary using available context data."""
        role = self.role.value
        meta = getattr(context, "metadata", {}) or {}
        blast = meta.get("blast_radius", [])
        root_causes = meta.get("root_causes", [])
        severity = meta.get("severity", "?")
        incident_id = getattr(context, "id", "unknown")
        rc_service = root_causes[0].get("service", "unknown") if root_causes else "unknown"
        rc_type = root_causes[0].get("type", "unknown") if root_causes else "unknown"
        # Also try SLO info from the trigger verdicts
        slo_info = ""
        if rc_service != "unknown" and rc_type != "unknown":
            slo_info = f"{rc_service} {rc_type}"

        if role == "triage":
            slo_info = f"{rc_service} {rc_type}" if rc_service != "unknown" else "incident"
            return f"DEGRADED: SEV-{severity} — {slo_info}, {len(blast)} services in blast radius"
        elif role == "investigation":
            return f"DEGRADED: Manual investigation required — root cause from correlation: {rc_service} ({rc_type})"
        elif role == "communication":
            return f"DEGRADED: Draft status update required for {incident_id}"
        elif role == "remediation":
            return f"DEGRADED: Manual remediation required — see correlation verdict for recommended actions"
        return f"DEGRADED: {role} — manual assessment required"

    # ------------------------------------------------------------------ #
    # Transport: governance                                                #
    # ------------------------------------------------------------------ #

    async def _request_autonomy_reduction(
        self,
        agent_name: str,
        arbiter_url: str,
        reason: str,
    ) -> dict:
        """POST to Arbiter's governance endpoint to reduce agent autonomy.

        Uses urllib (stdlib) in asyncio.to_thread; retries up to 3 times.
        """
        url = f"{arbiter_url}/api/v1/governance/reduce"
        payload = json.dumps({
            "agent": agent_name,
            "reason": reason,
        }).encode()

        def _sync_post() -> dict:
            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            last_exc: Exception | None = None
            for attempt in range(3):
                try:
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        return json.loads(resp.read().decode())
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
            raise RuntimeError(
                f"Autonomy reduction request failed after 3 attempts: {last_exc}"
            )

        return await asyncio.to_thread(_sync_post)

    # ------------------------------------------------------------------ #
    # Utility                                                              #
    # ------------------------------------------------------------------ #

    def _parse_json(self, response: str) -> dict:
        """Extract and parse JSON from a model response.

        Handles:
        - Clean JSON strings
        - Markdown fenced blocks (```json ... ```)
        - Preamble text before the first ``{``
        """
        text = response.strip()

        # Strip markdown fences
        fenced = re.sub(r"^```(?:json)?\s*", "", text)
        fenced = re.sub(r"\s*```$", "", fenced).strip()

        # Find a valid JSON object by matching braces
        brace_index = fenced.find("{")
        if brace_index == -1:
            raise ValueError(f"No JSON object found in response: {response!r}")

        # Try progressively from each '{' to find valid JSON
        for start in range(brace_index, len(fenced)):
            if fenced[start] != "{":
                continue
            depth = 0
            for end in range(start, len(fenced)):
                if fenced[end] == "{":
                    depth += 1
                elif fenced[end] == "}":
                    depth -= 1
                if depth == 0:
                    try:
                        return json.loads(fenced[start:end + 1])
                    except json.JSONDecodeError:
                        break  # try next '{'
                    break

        raise ValueError(f"Failed to parse JSON from response: {response!r}")

    # ------------------------------------------------------------------ #
    # Template method                                                      #
    # ------------------------------------------------------------------ #

    async def execute(self, context: IncidentContext) -> IncidentContext:
        """Run the agent against the given incident context.

        Template method — orchestrates the call sequence; subclasses
        provide judgment via build_prompt / parse_response / _apply_result.
        """
        try:
            system, user = self.build_prompt(context)
            response = await self._call_model(system, user)
            body_preview = response if len(response) <= 800 else response[:800].rsplit(" ", 1)[0] + "..."
            log.info("agent_response", role=self.role.value,
                     response_length=len(response), body=body_preview)
            result = self.parse_response(response, context)
            context = self._apply_result(context, result)
            confidence = getattr(result, "root_cause_confidence", None) or getattr(result, "confidence", 0.5)
            summary = self._build_summary(context, result)
            self._emit_verdict(
                context,
                subject_summary=summary,
                action="flag",
                confidence=confidence,
                reasoning=getattr(result, "reasoning", ""),
            )
            context = await self._post_execute(context, result)
        except Exception as exc:  # noqa: BLE001
            log.warning("agent_execute_failed", role=self.role.value,
                        error=str(exc), exc_info=True)
            self._degraded_verdict(context, str(exc))
        return context

    async def _post_execute(
        self, context: IncidentContext, result: Any
    ) -> IncidentContext:
        """Hook called after a successful execute cycle. No-op by default."""
        return context

    # ------------------------------------------------------------------ #
    # Abstract judgment interface                                          #
    # ------------------------------------------------------------------ #

    @abstractmethod
    def build_prompt(self, context: IncidentContext) -> tuple[str, str]:
        """Return (system_prompt, user_prompt) for this agent's judgment."""

    @abstractmethod
    def parse_response(self, response: str, context: IncidentContext) -> Any:
        """Parse the model's text response into a typed result object."""

    @abstractmethod
    def _apply_result(
        self, context: IncidentContext, result: Any
    ) -> IncidentContext:
        """Write the parsed result into the shared incident context."""
