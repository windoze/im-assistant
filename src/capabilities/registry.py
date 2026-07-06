"""Capability registry and three-tier directory loader."""

from __future__ import annotations

import importlib.util
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import ModuleType
from typing import Literal, Protocol

from src.capabilities.base import Capability, CapabilityOrigin

DEFAULT_SYSTEM_DIR = Path(__file__).with_name("system")
DEFAULT_BASE_DIR = Path(__file__).with_name("base")
DEFAULT_USER_ROOT = Path(__file__).with_name("user")
CAPABILITY_EXPORT_NAMES = ("CAPABILITIES", "capabilities", "CAPABILITY", "capability")
CapabilityMode = Literal["dm", "group"]


class CapabilityActorContext(Protocol):
    """Actor shape required by the capability visibility gate."""

    id: str


@dataclass(frozen=True, slots=True)
class CapabilityChannelContext:
    """Group channel capability switches loaded from administrator configuration."""

    id: str
    enabled_capabilities: Iterable[str] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        """Normalize the channel id and enabled capability names."""

        object.__setattr__(self, "id", _non_empty_string(self.id, "channel.id"))
        object.__setattr__(
            self,
            "enabled_capabilities",
            frozenset(
                _non_empty_string(name, "channel.enabled_capabilities")
                for name in self.enabled_capabilities
            ),
        )


class CapabilityRegistryError(RuntimeError):
    """Raised when capability discovery or registration fails."""


class CapabilityRegistry:
    """In-memory capability collection keyed by capability name."""

    def __init__(self, capabilities: Iterable[Capability] = ()) -> None:
        self._capabilities: dict[str, Capability] = {}
        for capability in capabilities:
            self.register(capability)

    def register(self, capability: Capability) -> None:
        """Register or replace a capability by name."""

        if not isinstance(capability, Capability):
            raise TypeError("capability must be a Capability instance")
        self._capabilities[capability.name] = capability

    def get(self, name: str) -> Capability | None:
        """Return one registered capability by name, if present."""

        return self._capabilities.get(name)

    def list(self) -> list[Capability]:
        """Return registered capabilities in deterministic name order."""

        return [self._capabilities[name] for name in sorted(self._capabilities)]

    def names(self) -> list[str]:
        """Return registered capability names in deterministic order."""

        return sorted(self._capabilities)


def can_use(
    capability: Capability,
    mode: CapabilityMode,
    actor: CapabilityActorContext,
    channel: CapabilityChannelContext | None,
) -> bool:
    """Return whether a capability passes the architecture §6.1 visibility gate."""

    if not isinstance(capability, Capability):
        raise TypeError("capability must be a Capability instance")
    if mode not in ("dm", "group"):
        raise ValueError("mode must be 'dm' or 'group'")

    actor_id = _non_empty_string(getattr(actor, "id", None), "actor.id")
    if capability.requires_user_authority and mode != "dm":
        return False
    if "global" in capability.available_in:
        return True
    if mode == "dm" and capability.origin == "user":
        return capability.owner_id == actor_id
    if mode == "group" and channel is not None:
        return capability.name in channel.enabled_capabilities
    return False


def load_capability_registry(
    *,
    system_dir: Path | str = DEFAULT_SYSTEM_DIR,
    base_dir: Path | str = DEFAULT_BASE_DIR,
    user_root: Path | str = DEFAULT_USER_ROOT,
    user_id: str | None = None,
) -> CapabilityRegistry:
    """Load capabilities from system, base, then one optional user tier."""

    registry = CapabilityRegistry()
    for capability in load_capabilities_from_directory(Path(system_dir), origin="system"):
        registry.register(capability)
    for capability in load_capabilities_from_directory(Path(base_dir), origin="base"):
        registry.register(capability)
    if user_id is not None:
        user_dir = Path(user_root) / user_id
        for capability in load_capabilities_from_directory(
            user_dir,
            origin="user",
            owner_id=user_id,
        ):
            registry.register(capability)
    return registry


def load_capabilities_from_directory(
    directory: Path | str,
    *,
    origin: CapabilityOrigin,
    owner_id: str | None = None,
) -> list[Capability]:
    """Discover capabilities exported by Python modules in one tier directory."""

    path = Path(directory)
    if not path.exists():
        return []
    if not path.is_dir():
        raise CapabilityRegistryError(f"Capability path is not a directory: {path}")

    capabilities: list[Capability] = []
    for module_file in sorted(path.glob("*.py")):
        if module_file.name == "__init__.py" or module_file.name.startswith("_"):
            continue
        module = _load_module_from_file(module_file)
        capabilities.extend(_capabilities_from_module(module, origin=origin, owner_id=owner_id))
    return capabilities


def _capabilities_from_module(
    module: ModuleType,
    *,
    origin: CapabilityOrigin,
    owner_id: str | None,
) -> list[Capability]:
    exported = _module_capability_exports(module)
    return [
        _capability_for_tier(capability, origin=origin, owner_id=owner_id)
        for capability in exported
    ]


def _module_capability_exports(module: ModuleType) -> list[Capability]:
    for name in CAPABILITY_EXPORT_NAMES:
        if hasattr(module, name):
            exported = getattr(module, name)
            return _normalize_capability_exports(
                exported, module_name=module.__name__, export_name=name
            )
    return []


def _normalize_capability_exports(
    exported: object,
    *,
    module_name: str,
    export_name: str,
) -> list[Capability]:
    if isinstance(exported, Capability):
        return [exported]
    if isinstance(exported, Mapping) or isinstance(exported, str):
        raise CapabilityRegistryError(
            f"{module_name}.{export_name} must be a Capability or iterable of Capability values"
        )
    try:
        capabilities = list(exported)  # type: ignore[arg-type]
    except TypeError as exc:
        raise CapabilityRegistryError(
            f"{module_name}.{export_name} must be a Capability or iterable of Capability values"
        ) from exc
    for capability in capabilities:
        if not isinstance(capability, Capability):
            raise CapabilityRegistryError(
                f"{module_name}.{export_name} contains a non-Capability value"
            )
    return capabilities


def _capability_for_tier(
    capability: Capability,
    *,
    origin: CapabilityOrigin,
    owner_id: str | None,
) -> Capability:
    if capability.origin != origin:
        raise CapabilityRegistryError(
            f"Capability {capability.name!r} declares origin {capability.origin!r} "
            f"but was loaded from {origin!r}"
        )
    if origin == "user" and capability.owner_id is None:
        return replace(capability, owner_id=owner_id)
    return capability


def _non_empty_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or value.strip() == "":
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _load_module_from_file(path: Path) -> ModuleType:
    module_name = f"_im_assistant_capability_{abs(hash(path.resolve()))}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise CapabilityRegistryError(f"Unable to load capability module: {path}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise CapabilityRegistryError(f"Failed to load capability module {path}: {exc}") from exc
    return module


__all__ = [
    "CapabilityActorContext",
    "CapabilityChannelContext",
    "CapabilityMode",
    "CapabilityRegistry",
    "CapabilityRegistryError",
    "can_use",
    "load_capabilities_from_directory",
    "load_capability_registry",
]
