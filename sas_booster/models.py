"""Validated models for reversible optimization sessions."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from typing import Any


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(slots=True)
class GameProfile:
    """A user-owned game entry with an exact executable identity."""

    id: str
    name: str
    platform: str = "Manual"
    executable: str = ""
    launch_executable: str = ""
    launch_arguments: list[str] = field(default_factory=list)
    launch_uri: str = ""
    install_root: str = ""
    app_id: str = ""
    settings: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GameProfile":
        clean = _known_fields(cls, data)
        clean["launch_arguments"] = [str(value) for value in clean.get("launch_arguments", [])]
        clean["settings"] = dict(clean.get("settings", {}))
        return cls(**clean)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _known_fields(model: type[Any], data: dict[str, Any]) -> dict[str, Any]:
    allowed = {item.name for item in fields(model)}
    return {key: value for key, value in data.items() if key in allowed}


@dataclass(slots=True)
class ProcessIdentity:
    pid: int
    name: str
    create_time: float
    priority: int | None = None
    affinity: list[int] = field(default_factory=list)
    action: str = ""
    executable: str = ""
    command_line: list[str] = field(default_factory=list)
    applied: bool = False

    @property
    def key(self) -> tuple[int, float]:
        return self.pid, self.create_time

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProcessIdentity":
        return cls(**_known_fields(cls, data))


@dataclass(slots=True)
class ClosedApplicationState:
    name: str
    executable: str
    arguments: list[str] = field(default_factory=list)
    working_directory: str = ""
    original_pid: int = 0
    original_create_time: float = 0.0
    applied: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ClosedApplicationState":
        return cls(**_known_fields(cls, data))


@dataclass(slots=True)
class RegistryValueState:
    hive: str
    path: str
    name: str
    existed: bool
    value: int | str | None = None
    value_type: int | None = None
    applied: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RegistryValueState":
        return cls(**_known_fields(cls, data))


@dataclass(slots=True)
class ServiceState:
    name: str
    was_running: bool
    applied: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ServiceState":
        return cls(**_known_fields(cls, data))


@dataclass(slots=True)
class PowerSettingState:
    scheme_guid: str
    subgroup_guid: str
    setting_guid: str
    ac_value: int | None = None
    dc_value: int | None = None
    applied: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PowerSettingState":
        return cls(**_known_fields(cls, data))


@dataclass(slots=True)
class PowerModeState:
    """Write-ahead state for one reversible Windows power-mode change."""

    original_ac_mode_guid: str | None = None
    original_dc_mode_guid: str | None = None
    original_plan_guid: str | None = None
    target_ac_mode_guid: str | None = None
    target_plan_guid: str | None = None
    on_ac_power: bool | None = None
    battery_percent: int | None = None
    battery_saver: bool | None = None
    modern_api_supported: bool = False
    applied: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PowerModeState":
        return cls(**_known_fields(cls, data))


@dataclass(slots=True)
class NetworkAdapterPowerState:
    name: str
    allow_turn_off: str
    selective_suspend: str
    applied: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NetworkAdapterPowerState":
        return cls(**_known_fields(cls, data))


@dataclass(slots=True)
class GameConfigState:
    path: str
    backup_path: str
    original_sha256: str
    optimized_sha256: str = ""
    original_values: dict[str, str] = field(default_factory=dict)
    applied: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GameConfigState":
        return cls(**_known_fields(cls, data))


@dataclass(slots=True)
class StateSnapshot:
    session_id: str
    schema_version: int = 3
    created_at: str = field(default_factory=utc_now)
    active: bool = True
    phase: str = "PREPARED"
    owner_pid: int = 0
    game_path: str = ""
    platform: str = ""
    launch_started_at: float = 0.0
    power_plan_guid: str | None = None
    registry_values: list[RegistryValueState] = field(default_factory=list)
    services: list[ServiceState] = field(default_factory=list)
    processes: list[ProcessIdentity] = field(default_factory=list)
    closed_applications: list[ClosedApplicationState] = field(default_factory=list)
    power_settings: list[PowerSettingState] = field(default_factory=list)
    power_mode: PowerModeState | None = None
    network_adapters: list[NetworkAdapterPowerState] = field(default_factory=list)
    game_config: GameConfigState | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    restored_at: str | None = None
    # Legacy v2 fields retained only so an unfinished older session can recover.
    game_mode_value: int | None = None
    game_mode_existed: bool = False
    paused_services: list[str] = field(default_factory=list)

    def validate(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.session_id):
            raise ValueError("Invalid session identifier")
        if self.schema_version not in {2, 3}:
            raise ValueError(f"Unsupported state schema {self.schema_version}")
        if self.phase not in {
            "PREPARED",
            "APPLYING",
            "ACTIVE",
            "RESTORING",
            "RESTORED",
            "RESTORE_INCOMPLETE",
        }:
            raise ValueError("Invalid session phase")
        if not isinstance(self.errors, list) or not isinstance(self.warnings, list):
            raise ValueError("Invalid session messages")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StateSnapshot":
        if not isinstance(data, dict):
            raise TypeError("Session state must be an object")
        copy = _known_fields(cls, dict(data))
        copy["processes"] = [ProcessIdentity.from_dict(item) for item in copy.get("processes", [])]
        copy["closed_applications"] = [
            ClosedApplicationState.from_dict(item) for item in copy.get("closed_applications", [])
        ]
        copy["registry_values"] = [
            RegistryValueState.from_dict(item) for item in copy.get("registry_values", [])
        ]
        copy["services"] = [ServiceState.from_dict(item) for item in copy.get("services", [])]
        copy["power_settings"] = [
            PowerSettingState.from_dict(item) for item in copy.get("power_settings", [])
        ]
        if isinstance(copy.get("power_mode"), dict):
            copy["power_mode"] = PowerModeState.from_dict(copy["power_mode"])
        copy["network_adapters"] = [
            NetworkAdapterPowerState.from_dict(item) for item in copy.get("network_adapters", [])
        ]
        if isinstance(copy.get("game_config"), dict):
            copy["game_config"] = GameConfigState.from_dict(copy["game_config"])
        if "schema_version" not in copy:
            copy["schema_version"] = 2
        if "phase" not in copy:
            copy["phase"] = "ACTIVE" if copy.get("active", True) else "RESTORED"
        snapshot = cls(**copy)
        snapshot.validate()
        return snapshot
