"""Tests for capability metadata and registry loading."""

from __future__ import annotations

import pytest

from src.capabilities import (
    Capability,
    CapabilityRegistry,
    CapabilityRegistryError,
    Requirement,
    load_capabilities_from_directory,
    load_capability_registry,
)


def test_capability_model_normalizes_metadata_and_detects_user_authority() -> None:
    """Capability metadata should be immutable, normalized, and OBO-aware."""

    requirement = Requirement(
        service="calendar",
        scopes=["calendar:read"],
        on_behalf_of="actor",
    )
    capability = Capability(
        name="schedule_summary",
        origin="system",
        available_in=["dm"],
        requires=[requirement],
        sensitivity="high",
        handler=lambda: "ok",
    )

    assert capability.available_in == ("dm",)
    assert capability.requires == (requirement,)
    assert requirement.scopes == ("calendar:read",)
    assert capability.requires_user_authority is True


def test_capability_model_rejects_invalid_metadata() -> None:
    """Malformed capability declarations should fail during construction."""

    with pytest.raises(ValueError, match="origin"):
        Capability(name="bad", origin="custom", available_in=["global"])

    with pytest.raises(ValueError, match="available_in"):
        Capability(name="bad", origin="system", available_in=[])

    with pytest.raises(ValueError, match="requires"):
        Capability(name="bad", origin="system", available_in=["global"], requires=["calendar"])


def test_registry_registers_replaces_and_lists_capabilities() -> None:
    """Manual registration should support listing and same-name replacement."""

    first = Capability(name="shared", origin="system", available_in=["global"])
    replacement = Capability(name="shared", origin="base", available_in=["global"])
    other = Capability(name="echo", origin="system", available_in=["dm"])
    registry = CapabilityRegistry([first, other])

    registry.register(replacement)

    assert registry.names() == ["echo", "shared"]
    assert registry.get("shared") == replacement
    assert registry.list() == [other, replacement]


def test_three_tier_loader_overlays_user_base_and_system_capabilities(tmp_path) -> None:
    """Directory loading should apply system, base, then user override order."""

    system_dir = tmp_path / "system"
    base_dir = tmp_path / "base"
    user_root = tmp_path / "user"
    user_dir = user_root / "user-1"
    system_dir.mkdir()
    base_dir.mkdir()
    user_dir.mkdir(parents=True)
    _write_capability_module(system_dir / "shared.py", name="shared", origin="system")
    _write_capability_module(base_dir / "shared.py", name="shared", origin="base")
    _write_capability_module(base_dir / "base_only.py", name="base_only", origin="base")
    _write_capability_module(user_dir / "shared.py", name="shared", origin="user")

    registry = load_capability_registry(
        system_dir=system_dir,
        base_dir=base_dir,
        user_root=user_root,
        user_id="user-1",
    )

    shared = registry.get("shared")
    base_only = registry.get("base_only")

    assert registry.names() == ["base_only", "shared"]
    assert shared is not None
    assert shared.origin == "user"
    assert shared.owner_id == "user-1"
    assert base_only is not None
    assert base_only.origin == "base"


def test_directory_loader_rejects_capability_with_wrong_tier_origin(tmp_path) -> None:
    """A module loaded from one tier must declare the matching capability origin."""

    base_dir = tmp_path / "base"
    base_dir.mkdir()
    _write_capability_module(base_dir / "wrong.py", name="wrong", origin="system")

    with pytest.raises(CapabilityRegistryError, match="declares origin"):
        load_capabilities_from_directory(base_dir, origin="base")


def test_directory_loader_ignores_missing_tier_directory(tmp_path) -> None:
    """Absent tier directories should simply contribute no capabilities."""

    missing_dir = tmp_path / "missing"

    assert load_capabilities_from_directory(missing_dir, origin="system") == []


def _write_capability_module(path, *, name: str, origin: str) -> None:
    path.write_text(
        f"""
from src.capabilities import Capability

def handle():
    return {name!r}

CAPABILITY = Capability(
    name={name!r},
    origin={origin!r},
    available_in=["global"],
    handler=handle,
)
""",
        encoding="utf-8",
    )
