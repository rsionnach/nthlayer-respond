# tests/test_config.py
"""Tests for nthlayer-respond configuration."""
import textwrap
from nthlayer_respond.config import RespondConfig, load_config


def test_default_config():
    cfg = RespondConfig()
    assert cfg.poll_interval_seconds == 30
    assert cfg.escalation_threshold == 0.3
    assert cfg.model == "claude-sonnet-4-20250514"
    assert cfg.triage_timeout == 15
    assert cfg.investigation_timeout == 60
    assert cfg.communication_timeout == 20
    assert cfg.remediation_timeout == 30
    assert cfg.root_cause_threshold == 0.7
    assert cfg.cooldown_seconds == 300
    assert cfg.arbiter_url == "http://localhost:8080"
    assert cfg.verdict_store_path == "verdicts.db"
    assert cfg.context_store_path == "respond-incidents.db"


def test_load_config_from_yaml(tmp_path):
    config_file = tmp_path / "mayday.yaml"
    config_file.write_text(textwrap.dedent("""\
        coordinator:
          poll_interval_seconds: 10
          escalation_threshold: 0.5
        agents:
          model: claude-opus-4-20250514
          triage:
            timeout: 10
          investigation:
            timeout: 45
            root_cause_threshold: 0.8
        safe_actions:
          arbiter_url: http://arbiter:9090
        verdict:
          store:
            path: /tmp/verdicts.db
        context_store:
          path: /tmp/incidents.db
    """))
    cfg = load_config(str(config_file))
    assert cfg.poll_interval_seconds == 10
    assert cfg.escalation_threshold == 0.5
    assert cfg.model == "claude-opus-4-20250514"
    assert cfg.triage_timeout == 10
    assert cfg.investigation_timeout == 45
    assert cfg.root_cause_threshold == 0.8
    assert cfg.arbiter_url == "http://arbiter:9090"
    assert cfg.verdict_store_path == "/tmp/verdicts.db"
    assert cfg.context_store_path == "/tmp/incidents.db"


def test_load_config_missing_file():
    cfg = load_config("/nonexistent/path.yaml")
    assert cfg.model == "claude-sonnet-4-20250514"  # defaults


def test_load_config_partial_yaml(tmp_path):
    config_file = tmp_path / "mayday.yaml"
    config_file.write_text("coordinator:\n  poll_interval_seconds: 5\n")
    cfg = load_config(str(config_file))
    assert cfg.poll_interval_seconds == 5
    assert cfg.model == "claude-sonnet-4-20250514"  # default preserved
