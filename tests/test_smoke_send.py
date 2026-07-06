"""Tests for the DingTalk smoke-send CLI."""

from __future__ import annotations

import pytest

from scripts import smoke_send


def test_parse_args_uses_positional_user_id(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """A positional userId should take precedence over environment fallbacks."""

    monkeypatch.setattr(smoke_send, "DEFAULT_ENV_PATH", tmp_path / ".env")
    monkeypatch.setenv(smoke_send.SMOKE_USER_ID_ENV, "env-user")

    args = smoke_send.parse_args([" user-1 ", "--text", "hello", "--department-id", "2"])

    assert args.user_id == "user-1"
    assert args.text == "hello"
    assert args.department_id == "2"


def test_parse_args_uses_environment_smoke_user_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """The smoke userId can be supplied through the process environment."""

    monkeypatch.setattr(smoke_send, "DEFAULT_ENV_PATH", tmp_path / ".env")
    monkeypatch.setenv(smoke_send.SMOKE_USER_ID_ENV, " env-user ")

    args = smoke_send.parse_args([])

    assert args.user_id == "env-user"


def test_parse_args_uses_dotenv_smoke_user_id(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """The smoke userId can be supplied through `.env` for repeatable local runs."""

    env_path = tmp_path / ".env"
    env_path.write_text(f"{smoke_send.SMOKE_USER_ID_ENV}=dotenv-user\n", encoding="utf-8")
    monkeypatch.setattr(smoke_send, "DEFAULT_ENV_PATH", env_path)
    monkeypatch.delenv(smoke_send.SMOKE_USER_ID_ENV, raising=False)

    args = smoke_send.parse_args([])

    assert args.user_id == "dotenv-user"


def test_parse_args_requires_user_id_when_no_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """The CLI should fail clearly when no smoke target userId is available."""

    monkeypatch.setattr(smoke_send, "DEFAULT_ENV_PATH", tmp_path / ".env")
    monkeypatch.delenv(smoke_send.SMOKE_USER_ID_ENV, raising=False)

    with pytest.raises(SystemExit) as exc_info:
        smoke_send.parse_args([])

    assert exc_info.value.code == 2
