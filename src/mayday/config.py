# src/mayday/config.py
"""Mayday configuration."""
from __future__ import annotations

from dataclasses import dataclass

import structlog
import yaml

logger = structlog.get_logger()


@dataclass
class MaydayConfig:
    # Coordinator
    poll_interval_seconds: int = 30
    escalation_threshold: float = 0.3
    # Agents
    model: str = "claude-sonnet-4-20250514"
    max_tokens: int = 4096
    triage_timeout: int = 15
    investigation_timeout: int = 60
    communication_timeout: int = 20
    remediation_timeout: int = 30
    root_cause_threshold: float = 0.7
    # Safe actions
    cooldown_seconds: int = 300
    arbiter_url: str = "http://localhost:8080"
    # Stores
    verdict_store_path: str = "verdicts.db"
    context_store_path: str = "mayday-incidents.db"
    # Topology
    manifests_dir: str | None = None


def load_config(path: str) -> MaydayConfig:
    """Load config from YAML file. Returns defaults if file missing."""
    try:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.info("config_not_found", path=path)
        return MaydayConfig()

    coord = data.get("coordinator", {})
    agents = data.get("agents", {})
    safe = data.get("safe_actions", {})
    verdict = data.get("verdict", {}).get("store", {})
    ctx_store = data.get("context_store", {})
    topo = data.get("topology", {})

    return MaydayConfig(
        poll_interval_seconds=coord.get("poll_interval_seconds", 30),
        escalation_threshold=coord.get("escalation_threshold", 0.3),
        model=agents.get("model", "claude-sonnet-4-20250514"),
        max_tokens=agents.get("max_tokens", 4096),
        triage_timeout=agents.get("triage", {}).get("timeout", 15),
        investigation_timeout=agents.get("investigation", {}).get("timeout", 60),
        communication_timeout=agents.get("communication", {}).get("timeout", 20),
        remediation_timeout=agents.get("remediation", {}).get("timeout", 30),
        root_cause_threshold=agents.get("investigation", {}).get("root_cause_threshold", 0.7),
        cooldown_seconds=safe.get("cooldown_seconds", 300),
        arbiter_url=safe.get("arbiter_url", "http://localhost:8080"),
        verdict_store_path=verdict.get("path", "verdicts.db"),
        context_store_path=ctx_store.get("path", "mayday-incidents.db"),
        manifests_dir=topo.get("manifests_dir"),
    )
