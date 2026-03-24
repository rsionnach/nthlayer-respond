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
        self._client = None  # lazy-init Anthropic client

    # ------------------------------------------------------------------ #
    # Transport: model                                                     #
    # ------------------------------------------------------------------ #

    async def _call_model(self, system_prompt: str, user_prompt: str) -> str:
        """Call the Anthropic API asynchronously.

        Lazy-initialises the synchronous Anthropic client, then offloads
        the blocking call to a thread via asyncio.to_thread, wrapped in
        asyncio.wait_for for timeout enforcement.
        """
        if self._client is None:
            import anthropic  # deferred import — not available in tests
            self._client = anthropic.Anthropic()

        def _sync_call() -> str:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
            return response.content[0].text

        return await asyncio.wait_for(
            asyncio.to_thread(_sync_call),
            timeout=self._timeout,
        )

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

    def _degraded_verdict(self, context: IncidentContext, reason: str) -> Verdict:
        """Emit a degraded escalation verdict when the agent cannot produce a
        normal judgment (e.g. model timeout, API error)."""
        return self._emit_verdict(
            context,
            subject_summary=f"{self.role.value} degraded — human takeover required",
            action="escalate",
            confidence=0.0,
            reasoning=f"Agent operating in degraded mode: {reason}",
            tags=["degraded", "human-takeover-required"],
        )

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
            result = self.parse_response(response, context)
            context = self._apply_result(context, result)
            confidence = getattr(result, "root_cause_confidence", None) or getattr(result, "confidence", 0.5)
            self._emit_verdict(
                context,
                subject_summary=f"{self.role.value} assessment",
                action="flag",
                confidence=confidence,
                reasoning=getattr(result, "reasoning", ""),
            )
            context = await self._post_execute(context, result)
        except Exception as exc:  # noqa: BLE001
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
