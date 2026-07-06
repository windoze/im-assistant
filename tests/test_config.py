"""Tests for typed configuration loading."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from src.infra.config import ConfigError, load_config

FERNET_KEY = Fernet.generate_key().decode("utf-8")
REQUIRED_ENV = {
    "DINGTALK_APP_KEY": "app-key",
    "DINGTALK_APP_SECRET": "app-secret",
    "DINGTALK_ROBOT_CODE": "robot-code",
    "ANTHROPIC_API_KEY": "anthropic-key",
    "OAUTH_REDIRECT_URI": "https://example.com/oauth/callback",
    "TOKEN_VAULT_FERNET_KEY": FERNET_KEY,
}


def test_load_config_merges_env_and_yaml(tmp_path) -> None:
    """Configuration should combine `.env` secrets with YAML settings."""

    config_path = tmp_path / "config.yaml"
    env_path = tmp_path / ".env"
    config_path.write_text(
        """
llm:
  model: claude-opus-test
session:
  confirm_timeout_sec: 42
storage:
  database_path: state/assistant.db
capabilities:
  channel_enabled:
    group-open-conversation-id:
      - create_doc
      - contact_lookup
dingtalk:
  api_base: https://api.example.com/
  legacy_api_base: https://oapi.example.com/
  document:
    parent_object_type: wiki_space
    parent_object_id: space-1
logging:
  level: debug
""",
        encoding="utf-8",
    )
    env_path.write_text(
        "\n".join(f"{key}={value}" for key, value in REQUIRED_ENV.items()),
        encoding="utf-8",
    )

    config = load_config(config_path=config_path, env_path=env_path, environ={})

    assert config.dingtalk.app_key == "app-key"
    assert config.dingtalk.app_secret == "app-secret"
    assert config.dingtalk.robot_code == "robot-code"
    assert config.dingtalk.api_base == "https://api.example.com"
    assert config.dingtalk.legacy_api_base == "https://oapi.example.com"
    assert config.dingtalk.document.parent_object_type == "wiki_space"
    assert config.dingtalk.document.parent_object_id == "space-1"
    assert config.llm.model == "claude-opus-test"
    assert config.llm.anthropic_api_key == "anthropic-key"
    assert config.session.confirm_timeout_sec == 42
    assert config.storage.database_path == tmp_path / "state" / "assistant.db"
    assert config.token_vault.fernet_key == FERNET_KEY
    assert config.capabilities.channel_enabled_capabilities == {
        "group-open-conversation-id": ("create_doc", "contact_lookup")
    }
    assert config.logging.level == "DEBUG"
    assert config.oauth.redirect_uri == "https://example.com/oauth/callback"


def test_load_config_missing_required_values_reports_names(tmp_path) -> None:
    """Missing required secrets should raise a clear configuration error."""

    config_path = tmp_path / "config.yaml"
    env_path = tmp_path / ".env"
    config_path.write_text("llm: {}\n", encoding="utf-8")
    env_path.write_text(
        "DINGTALK_APP_KEY=app-key\nDINGTALK_ROBOT_CODE=robot-code\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as exc_info:
        load_config(config_path=config_path, env_path=env_path, environ={})

    message = str(exc_info.value)
    assert "DINGTALK_APP_SECRET" in message
    assert "ANTHROPIC_API_KEY" in message
    assert "OAUTH_REDIRECT_URI" in message
    assert "TOKEN_VAULT_FERNET_KEY" in message


def test_load_config_rejects_invalid_channel_enabled_capabilities(tmp_path) -> None:
    """Channel-enabled capability settings must map channel ids to name lists."""

    config_path = tmp_path / "config.yaml"
    env_path = tmp_path / ".env"
    config_path.write_text(
        """
capabilities:
  channel_enabled:
    group-open-conversation-id: create_doc
""",
        encoding="utf-8",
    )
    env_path.write_text(
        "\n".join(f"{key}={value}" for key, value in REQUIRED_ENV.items()),
        encoding="utf-8",
    )

    with pytest.raises(
        ConfigError, match="capabilities.channel_enabled.group-open-conversation-id"
    ):
        load_config(config_path=config_path, env_path=env_path, environ={})


def test_load_config_rejects_partial_dingtalk_document_defaults(tmp_path) -> None:
    """Document parent defaults must be configured as a complete pair."""

    config_path = tmp_path / "config.yaml"
    env_path = tmp_path / ".env"
    config_path.write_text(
        """
dingtalk:
  document:
    parent_object_id: space-1
""",
        encoding="utf-8",
    )
    env_path.write_text(
        "\n".join(f"{key}={value}" for key, value in REQUIRED_ENV.items()),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="dingtalk.document.parent_object_type"):
        load_config(config_path=config_path, env_path=env_path, environ={})
