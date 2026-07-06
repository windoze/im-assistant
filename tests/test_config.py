"""Tests for typed configuration loading."""

from __future__ import annotations

import pytest

from src.infra.config import ConfigError, load_config

REQUIRED_ENV = {
    "DINGTALK_APP_KEY": "app-key",
    "DINGTALK_APP_SECRET": "app-secret",
    "DINGTALK_ROBOT_CODE": "robot-code",
    "ANTHROPIC_API_KEY": "anthropic-key",
    "OAUTH_REDIRECT_URI": "https://example.com/oauth/callback",
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
dingtalk:
  api_base: https://api.example.com/
  legacy_api_base: https://oapi.example.com/
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
    assert config.llm.model == "claude-opus-test"
    assert config.llm.anthropic_api_key == "anthropic-key"
    assert config.session.confirm_timeout_sec == 42
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
