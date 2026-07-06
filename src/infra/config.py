"""Typed application configuration loaded from `.env` and `config.yaml`."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml
from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"

REQUIRED_ENV_VARS = (
    "DINGTALK_APP_KEY",
    "DINGTALK_APP_SECRET",
    "DINGTALK_ROBOT_CODE",
    "ANTHROPIC_API_KEY",
    "OAUTH_REDIRECT_URI",
    "TOKEN_VAULT_FERNET_KEY",
)


class ConfigError(ValueError):
    """Raised when required configuration is missing or malformed."""


@dataclass(frozen=True, slots=True)
class DingTalkDocumentConfig:
    """Default DingTalk document parent for app-level document creation."""

    parent_object_type: str | None = None
    parent_object_id: str | None = None


@dataclass(frozen=True, slots=True)
class DingTalkConfig:
    """DingTalk application credentials and OpenAPI endpoints."""

    app_key: str
    app_secret: str = field(repr=False)
    robot_code: str
    api_base: str
    legacy_api_base: str
    document: DingTalkDocumentConfig = field(default_factory=DingTalkDocumentConfig)


@dataclass(frozen=True, slots=True)
class LLMConfig:
    """Claude model and API key settings."""

    model: str
    anthropic_api_key: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class SessionConfig:
    """Runtime session behavior settings."""

    confirm_timeout_sec: int


@dataclass(frozen=True, slots=True)
class StorageConfig:
    """SQLite storage settings."""

    database_path: Path


@dataclass(frozen=True, slots=True)
class TokenVaultConfig:
    """Fernet key used to encrypt delegated user tokens at rest."""

    fernet_key: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class CapabilitiesConfig:
    """Capability visibility settings controlled outside source code."""

    channel_enabled_capabilities: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    """Structured logging settings."""

    level: str


@dataclass(frozen=True, slots=True)
class OAuthConfig:
    """OAuth redirect settings for user authorization flows."""

    redirect_uri: str


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Complete typed application configuration."""

    dingtalk: DingTalkConfig
    llm: LLMConfig
    session: SessionConfig
    storage: StorageConfig
    token_vault: TokenVaultConfig
    capabilities: CapabilitiesConfig
    logging: LoggingConfig
    oauth: OAuthConfig


def load_config(
    *,
    config_path: str | Path = DEFAULT_CONFIG_PATH,
    env_path: str | Path = DEFAULT_ENV_PATH,
    environ: Mapping[str, str] | None = None,
) -> AppConfig:
    """Load `.env` secrets and `config.yaml` settings into typed dataclasses."""

    config_file = Path(config_path)
    dotenv_file = Path(env_path)
    raw_config = _read_yaml_config(config_file)
    env_values = _read_env_values(dotenv_file, environ)

    missing = [name for name in REQUIRED_ENV_VARS if not _has_value(env_values.get(name))]
    if missing:
        raise ConfigError(f"Missing required configuration values: {', '.join(missing)}")

    dingtalk_section = _section(raw_config, "dingtalk")
    dingtalk_document_section = _section(dingtalk_section, "document")
    llm_section = _section(raw_config, "llm")
    session_section = _section(raw_config, "session")
    storage_section = _section(raw_config, "storage")
    capabilities_section = _section(raw_config, "capabilities")
    logging_section = _section(raw_config, "logging")

    return AppConfig(
        dingtalk=DingTalkConfig(
            app_key=_required_env(env_values, "DINGTALK_APP_KEY"),
            app_secret=_required_env(env_values, "DINGTALK_APP_SECRET"),
            robot_code=_required_env(env_values, "DINGTALK_ROBOT_CODE"),
            api_base=_url_string(dingtalk_section, "api_base", "https://api.dingtalk.com"),
            legacy_api_base=_url_string(
                dingtalk_section,
                "legacy_api_base",
                "https://oapi.dingtalk.com",
            ),
            document=_dingtalk_document_config(dingtalk_document_section),
        ),
        llm=LLMConfig(
            model=_non_empty_string(llm_section, "model", "claude-sonnet-5"),
            anthropic_api_key=_required_env(env_values, "ANTHROPIC_API_KEY"),
        ),
        session=SessionConfig(
            confirm_timeout_sec=_positive_int(session_section, "confirm_timeout_sec", 1800),
        ),
        storage=StorageConfig(
            database_path=_path_value(
                storage_section,
                "database_path",
                "im_assistant.db",
                base_dir=config_file.parent,
            ),
        ),
        token_vault=TokenVaultConfig(
            fernet_key=_required_env(env_values, "TOKEN_VAULT_FERNET_KEY"),
        ),
        capabilities=CapabilitiesConfig(
            channel_enabled_capabilities=_channel_enabled_capabilities(capabilities_section),
        ),
        logging=LoggingConfig(
            level=_non_empty_string(logging_section, "level", "INFO").upper(),
        ),
        oauth=OAuthConfig(
            redirect_uri=_required_env(env_values, "OAUTH_REDIRECT_URI"),
        ),
    )


def _read_yaml_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in config file {path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"Unable to read config file {path}: {exc}") from exc

    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"Config file {path} must contain a mapping at the top level")
    return raw


def _read_env_values(path: Path, environ: Mapping[str, str] | None) -> dict[str, str]:
    values = {key: value for key, value in dotenv_values(path).items() if value is not None}
    source_env = os.environ if environ is None else environ
    values.update({key: value for key, value in source_env.items() if value is not None})
    return values


def _section(config: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = config.get(key, {})
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigError(f"`{key}` must be a mapping")
    return value


def _has_value(value: str | None) -> bool:
    return value is not None and value.strip() != ""


def _required_env(env_values: Mapping[str, str], key: str) -> str:
    value = env_values.get(key)
    if not _has_value(value):
        raise ConfigError(f"Missing required configuration value: {key}")
    return value.strip()


def _non_empty_string(section: Mapping[str, Any], key: str, default: str) -> str:
    value = section.get(key, default)
    if not isinstance(value, str) or value.strip() == "":
        raise ConfigError(f"`{key}` must be a non-empty string")
    return value.strip()


def _url_string(section: Mapping[str, Any], key: str, default: str) -> str:
    return _non_empty_string(section, key, default).rstrip("/")


def _optional_non_empty_string(section: Mapping[str, Any], key: str) -> str | None:
    value = section.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or value.strip() == "":
        raise ConfigError(f"`{key}` must be a non-empty string when provided")
    return value.strip()


def _dingtalk_document_config(section: Mapping[str, Any]) -> DingTalkDocumentConfig:
    parent_object_type = _optional_non_empty_string(section, "parent_object_type")
    parent_object_id = _optional_non_empty_string(section, "parent_object_id")
    if (parent_object_type is None) != (parent_object_id is None):
        raise ConfigError(
            "`dingtalk.document.parent_object_type` and "
            "`dingtalk.document.parent_object_id` must be configured together"
        )
    return DingTalkDocumentConfig(
        parent_object_type=parent_object_type,
        parent_object_id=parent_object_id,
    )


def _path_value(section: Mapping[str, Any], key: str, default: str, *, base_dir: Path) -> Path:
    value = _non_empty_string(section, key, default)
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path


def _channel_enabled_capabilities(section: Mapping[str, Any]) -> Mapping[str, tuple[str, ...]]:
    raw_mapping = section.get("channel_enabled", {})
    if raw_mapping is None:
        return MappingProxyType({})
    if not isinstance(raw_mapping, Mapping):
        raise ConfigError("`capabilities.channel_enabled` must be a mapping")

    enabled_by_channel: dict[str, tuple[str, ...]] = {}
    for raw_channel_id, raw_capabilities in raw_mapping.items():
        channel_id = _config_string(raw_channel_id, "capabilities.channel_enabled channel id")
        if isinstance(raw_capabilities, str) or not isinstance(raw_capabilities, Sequence):
            raise ConfigError(
                f"`capabilities.channel_enabled.{channel_id}` must be a sequence of "
                "capability names"
            )
        capability_names = tuple(
            dict.fromkeys(
                _config_string(name, f"capabilities.channel_enabled.{channel_id}")
                for name in raw_capabilities
            )
        )
        enabled_by_channel[channel_id] = capability_names
    return MappingProxyType(enabled_by_channel)


def _positive_int(section: Mapping[str, Any], key: str, default: int) -> int:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"`{key}` must be an integer")
    if value <= 0:
        raise ConfigError(f"`{key}` must be greater than 0")
    return value


def _config_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or value.strip() == "":
        raise ConfigError(f"`{field_name}` must be a non-empty string")
    return value.strip()
