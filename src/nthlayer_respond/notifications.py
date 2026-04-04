"""Slack block builders for nthlayer-respond incident lifecycle verdicts."""
from __future__ import annotations

import os
import logging
from typing import Any

logger = logging.getLogger(__name__)


def build_triage_blocks(verdict, context=None) -> tuple[list[dict], str]:
    """Build Slack blocks for incident triage notification."""
    summary = verdict.subject.summary or ""
    first_sentence = summary.split(".")[0] if summary else "Incident opened"
    confidence = verdict.judgment.confidence

    text = f"\U0001f6a8 {first_sentence}"

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": "*\U0001f6a8 INCIDENT OPENED*"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": first_sentence}},
        {"type": "context", "elements": [
            {"type": "mrkdwn", "text": f"nthlayer-respond \u00b7 confidence {confidence:.2f} \u00b7 {verdict.id}"},
        ]},
    ]
    return blocks, text


def build_remediation_blocks(verdict, context=None) -> tuple[list[dict], str]:
    """Build Slack blocks for remediation proposal."""
    summary = verdict.subject.summary or "Remediation proposed"
    first_sentence = summary.split(".")[0] if summary else "Remediation proposed"
    confidence = verdict.judgment.confidence

    text = f"\U0001f527 {first_sentence}"

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": "*\U0001f527 REMEDIATION*"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": first_sentence}},
        {"type": "context", "elements": [
            {"type": "mrkdwn", "text": f"nthlayer-respond \u00b7 confidence {confidence:.2f} \u00b7 {verdict.id}"},
        ]},
    ]
    return blocks, text


def build_approval_blocks(verdict, incident_id: str, context=None) -> tuple[list[dict], str]:
    """Build Slack blocks for remediation approval request with interactive buttons."""
    summary = verdict.subject.summary or "Remediation proposed"
    first_sentence = summary.split(".")[0] if summary else "Remediation proposed"
    confidence = verdict.judgment.confidence

    text = f"\u2757 APPROVAL REQUIRED: {first_sentence}"

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": "*\u2757 APPROVAL REQUIRED*"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": first_sentence}},
        {"type": "context", "elements": [
            {"type": "mrkdwn", "text": f"nthlayer-respond \u00b7 confidence {confidence:.2f} \u00b7 {verdict.id}"},
        ]},
        {
            "type": "actions",
            "block_id": f"approval_{incident_id}",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "style": "primary",
                    "action_id": "approve",
                    "value": incident_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Reject"},
                    "style": "danger",
                    "action_id": "reject",
                    "value": incident_id,
                },
            ],
        },
    ]
    return blocks, text


def build_verification_blocks(verdict, verified: bool | None = None) -> tuple[list[dict], str]:
    """Build Slack blocks for remediation verification result."""
    if verified is True:
        emoji = "\u2705"
        label = "VERIFIED"
    elif verified is False:
        emoji = "\u274c"
        label = "VERIFICATION FAILED"
    else:
        emoji = "\u2753"
        label = "VERIFICATION UNKNOWN"

    summary = verdict.subject.summary or ""
    text = f"{emoji} {label}: {summary[:80]}"

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{emoji} {label}*"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": summary[:200] if summary else "See verdict for details."}},
        {"type": "context", "elements": [
            {"type": "mrkdwn", "text": f"nthlayer-respond \u00b7 {verdict.id}"},
        ]},
    ]
    return blocks, text


def build_resolution_blocks(verdict, context=None) -> tuple[list[dict], str]:
    """Build Slack blocks for incident resolution."""
    text = "\u2705 Incident resolved \u2014 full verdict chain in NthLayer"

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": "*\u2705 INCIDENT RESOLVED*"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "Full verdict chain: evaluate \u2192 correlate \u2192 triage \u2192 investigate \u2192 remediate \u2192 learn"}},
        {"type": "context", "elements": [
            {"type": "mrkdwn", "text": f"nthlayer-respond \u00b7 {verdict.id}"},
        ]},
    ]
    return blocks, text


def find_slack_thread_ts(verdict_store, verdict_ids: list[str]) -> str | None:
    """Walk verdict lineage to find slack_thread_ts.

    Returns None if no thread_ts found (graceful degradation).
    """
    for vid in verdict_ids:
        try:
            v = verdict_store.get(vid)
            if v is None:
                continue
            custom = getattr(v.metadata, "custom", {}) or {}
            ts = custom.get("slack_thread_ts")
            if ts:
                return ts
            for ctx_id in (v.lineage.context or []):
                try:
                    ctx_v = verdict_store.get(ctx_id)
                    if ctx_v:
                        ctx_custom = getattr(ctx_v.metadata, "custom", {}) or {}
                        ts = ctx_custom.get("slack_thread_ts")
                        if ts:
                            return ts
                except Exception:
                    pass
        except Exception:
            pass
    return None


def resolve_slack_channel(context, env_fallback: str | None = None) -> str | None:
    """Resolve Slack channel ID from service manifest or env var.

    Resolution order:
    1. Manifest metadata.service_context.spec.ownership.slack_channel
    2. SLACK_CHANNEL_ID env var — or env_fallback if provided (empty string
       suppresses env var lookup and returns None)
    3. None (no channel configured)
    """
    service_ctx = context.metadata.get("service_context", {}) if isinstance(context.metadata, dict) else {}
    spec = service_ctx.get("spec", {})
    ownership = spec.get("ownership", {})
    channel = ownership.get("slack_channel")
    if channel:
        return channel

    if env_fallback is not None:
        return env_fallback or None
    return os.environ.get("SLACK_CHANNEL_ID") or None


async def send_slack_notification(
    verdict,
    block_builder,
    verdict_store=None,
    trigger_verdict_ids: list[str] | None = None,
    **builder_kwargs,
) -> None:
    """Send a Slack notification for a verdict, threading if possible.

    Fail-open: if Slack is not configured or unreachable, silently returns.
    """
    slack_url = os.environ.get("SLACK_WEBHOOK_URL", "")
    if not slack_url:
        return

    from nthlayer_common.slack import SlackNotifier

    thread_ts = None
    if verdict_store and trigger_verdict_ids:
        thread_ts = find_slack_thread_ts(verdict_store, trigger_verdict_ids)

    blocks, text = block_builder(verdict, **builder_kwargs)
    notifier = SlackNotifier(slack_url)
    new_ts = await notifier.send(blocks, text, thread_ts=thread_ts)

    # Store thread_ts if we started a new thread
    if new_ts and not thread_ts:
        try:
            custom = getattr(verdict.metadata, "custom", None)
            if custom is not None:
                custom["slack_thread_ts"] = new_ts
                verdict_store.put(verdict)
        except Exception:
            pass
