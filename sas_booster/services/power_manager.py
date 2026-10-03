"""Safe, reversible Windows power-mode management.

Windows 11 exposes user-configured AC/DC power modes through documented
``powrprof.dll`` APIs.  This module changes the AC mode only; it never raises
the DC (battery) mode.  Older Windows versions fall back to an already-existing
performance power plan and never create or duplicate a plan.
"""

from __future__ import annotations

import ctypes
import uuid
from dataclasses import dataclass, field
from typing import Protocol

from sas_booster.constants import HIGH_PERFORMANCE_GUID, ULTIMATE_PERFORMANCE_GUID
from sas_booster.models import PowerModeState
from sas_booster.utils.windows import (
    available_power_plans,
    current_power_plan,
    is_windows,
    set_power_plan,
    system_power_status,
)


BEST_EFFICIENCY_MODE_GUID = "961cc777-2547-4f9d-8174-7d86181b8a7a"
BALANCED_MODE_GUID = "00000000-0000-0000-0000-000000000000"
BEST_PERFORMANCE_MODE_GUID = "ded574b5-45a0-4f42-8737-46345c09c238"

_ERROR_SUCCESS = 0
_ERROR_NOT_SUPPORTED = 50
_ERROR_CALL_NOT_IMPLEMENTED = 120
_ERROR_PROC_NOT_FOUND = 127
_UNSUPPORTED_ERRORS = {
    _ERROR_NOT_SUPPORTED,
    _ERROR_CALL_NOT_IMPLEMENTED,
    _ERROR_PROC_NOT_FOUND,
}


class PowerManagerError(RuntimeError):
    """A supported power operation failed and must not be silently ignored."""


class PowerModeApiUnavailable(PowerManagerError):
    """The documented Windows 11 power-mode API is unavailable."""


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def from_text(cls, value: str) -> "_GUID":
        try:
            raw = uuid.UUID(value).bytes_le
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError(f"Invalid power-mode GUID: {value!r}") from exc
        return cls(
            int.from_bytes(raw[0:4], "little"),
            int.from_bytes(raw[4:6], "little"),
            int.from_bytes(raw[6:8], "little"),
            (ctypes.c_ubyte * 8).from_buffer_copy(raw[8:16]),
        )

    def to_text(self) -> str:
        raw = ctypes.string_at(ctypes.byref(self), ctypes.sizeof(self))
        return str(uuid.UUID(bytes_le=raw))


class PowerModeApi(Protocol):
    """Small protocol so the native boundary can be tested without mutations."""

    def get_ac_mode(self) -> str: ...

    def get_dc_mode(self) -> str: ...

    def set_ac_mode(self, guid: str) -> None: ...


class _NativePowerModeApi:
    def __init__(self) -> None:
        if not is_windows():
            raise PowerModeApiUnavailable("Windows power modes are unavailable")
        loader = getattr(ctypes, "WinDLL", None)
        if loader is None:
            raise PowerModeApiUnavailable("The Windows DLL loader is unavailable")
        try:
            self._library = loader("powrprof.dll", use_last_error=True)
            self._get_ac = self._library.PowerGetUserConfiguredACPowerMode
            self._get_dc = self._library.PowerGetUserConfiguredDCPowerMode
            self._set_ac = self._library.PowerSetUserConfiguredACPowerMode
        except (AttributeError, OSError) as exc:
            raise PowerModeApiUnavailable(
                "This Windows version does not expose user-configured power modes"
            ) from exc

        pointer = ctypes.POINTER(_GUID)
        self._get_ac.argtypes = [pointer]
        self._get_ac.restype = ctypes.c_uint32
        self._get_dc.argtypes = [pointer]
        self._get_dc.restype = ctypes.c_uint32
        self._set_ac.argtypes = [pointer]
        self._set_ac.restype = ctypes.c_uint32

    @staticmethod
    def _check(result: int, operation: str) -> None:
        code = int(result)
        if code == _ERROR_SUCCESS:
            return
        if code in _UNSUPPORTED_ERRORS:
            raise PowerModeApiUnavailable(
                f"{operation} is unsupported (Windows error {code})"
            )
        raise PowerManagerError(f"{operation} failed with Windows error {code}")

    def get_ac_mode(self) -> str:
        value = _GUID()
        self._check(self._get_ac(ctypes.byref(value)), "Read AC power mode")
        return value.to_text()

    def get_dc_mode(self) -> str:
        value = _GUID()
        self._check(self._get_dc(ctypes.byref(value)), "Read DC power mode")
        return value.to_text()

    def set_ac_mode(self, guid: str) -> None:
        value = _GUID.from_text(guid)
        self._check(self._set_ac(ctypes.byref(value)), "Set AC power mode")


@dataclass(slots=True)
class PowerActionResult:
    changed: bool
    message: str
    warnings: list[str] = field(default_factory=list)


class PowerPlanManager:
    """Prepare, apply, and restore one power optimization transaction.

    ``snapshot`` prepares write-ahead targets but changes nothing.  Persist the
    returned :class:`PowerModeState` before calling ``apply``.  ``restore`` is
    idempotent and reasserts the original AC mode/plan even if a crash occurred
    between the native mutation and marking ``applied``.
    """

    def __init__(self, api: PowerModeApi | None = None) -> None:
        self._api = api
        self._api_checked = api is not None

    def _native_api(self) -> PowerModeApi | None:
        if not self._api_checked:
            self._api_checked = True
            try:
                self._api = _NativePowerModeApi()
            except PowerModeApiUnavailable:
                self._api = None
        return self._api

    @staticmethod
    def _normalized_guid(value: str | None) -> str | None:
        if not value:
            return None
        try:
            return str(uuid.UUID(value))
        except (AttributeError, TypeError, ValueError) as exc:
            raise PowerManagerError(f"Invalid saved power GUID: {value!r}") from exc

    @staticmethod
    def _battery_warnings(
        on_ac_power: bool | None,
        battery_percent: int | None,
        battery_saver: bool | None,
    ) -> list[str]:
        warnings: list[str] = []
        if on_ac_power is False:
            charge = f" ({battery_percent}% remaining)" if battery_percent is not None else ""
            warnings.append(
                "Running on battery"
                f"{charge}; Best Performance was not applied. Connect the AC adapter."
            )
        elif on_ac_power is None:
            warnings.append("Power source could not be verified; power changes were skipped.")
        if battery_saver:
            warnings.append(
                "Windows Battery Saver is active; turn it off in Windows Settings before playing."
            )
        return warnings

    def snapshot(self) -> PowerModeState:
        """Capture exact AC/DC modes and prepare a safe write-ahead target."""

        status = system_power_status()
        plan_guid, _ = current_power_plan()
        plan_guid = self._normalized_guid(plan_guid)
        api = self._native_api()

        ac_mode: str | None = None
        dc_mode: str | None = None
        modern_supported = api is not None
        if api is not None:
            try:
                ac_mode = self._normalized_guid(api.get_ac_mode())
                dc_mode = self._normalized_guid(api.get_dc_mode())
            except PowerModeApiUnavailable:
                # Only unsupported APIs may use the legacy plan fallback.
                api = None
                self._api = None
                modern_supported = False

        state = PowerModeState(
            original_ac_mode_guid=ac_mode,
            original_dc_mode_guid=dc_mode,
            original_plan_guid=plan_guid,
            on_ac_power=status.on_ac_power,
            battery_percent=status.battery_percent,
            battery_saver=status.battery_saver,
            modern_api_supported=modern_supported,
        )

        if status.on_ac_power is not True:
            return state
        if api is not None:
            if ac_mode != BEST_PERFORMANCE_MODE_GUID:
                state.target_ac_mode_guid = BEST_PERFORMANCE_MODE_GUID
            return state

        # Windows 10 fallback: select only a plan already registered by the OS
        # or OEM.  Never duplicate or manufacture Ultimate Performance.
        # If the active plan cannot be identified exactly, changing it would
        # leave no trustworthy value to restore after the session.
        if plan_guid is None:
            return state
        plans = available_power_plans()
        for candidate in (HIGH_PERFORMANCE_GUID, ULTIMATE_PERFORMANCE_GUID):
            candidate = candidate.lower()
            if candidate in plans and candidate != plan_guid:
                state.target_plan_guid = candidate
                break
        return state

    def apply(self, state: PowerModeState) -> PowerActionResult:
        """Apply the prepared AC-only target and verify the resulting GUID."""

        current_status = system_power_status()
        warnings = self._battery_warnings(
            current_status.on_ac_power,
            current_status.battery_percent,
            current_status.battery_saver,
        )
        if current_status.on_ac_power is not True:
            state.applied = False
            return PowerActionResult(False, "Power optimization skipped", warnings)

        if state.target_ac_mode_guid:
            api = self._native_api()
            if api is None:
                raise PowerModeApiUnavailable(
                    "The AC power-mode API became unavailable after the snapshot"
                )
            target = self._normalized_guid(state.target_ac_mode_guid)
            if target != BEST_PERFORMANCE_MODE_GUID:
                raise PowerManagerError("Refusing an unsupported AC power-mode target")
            current = self._normalized_guid(api.get_ac_mode())
            changed = current != target
            if changed:
                api.set_ac_mode(target)
            verified = self._normalized_guid(api.get_ac_mode())
            if verified != target:
                raise PowerManagerError("Windows did not retain the requested AC power mode")
            state.applied = changed
            return PowerActionResult(
                changed,
                "Windows AC power mode set to Best Performance",
                warnings,
            )

        if state.target_plan_guid:
            target = self._normalized_guid(state.target_plan_guid)
            if target not in {HIGH_PERFORMANCE_GUID, ULTIMATE_PERFORMANCE_GUID}:
                raise PowerManagerError("Refusing an unsupported fallback power plan")
            plans = available_power_plans()
            if target not in plans:
                raise PowerManagerError("The prepared fallback power plan is no longer available")
            current, _ = current_power_plan()
            current = self._normalized_guid(current)
            changed = current != target
            if changed:
                set_power_plan(target)
            verified, _ = current_power_plan()
            if self._normalized_guid(verified) != target:
                raise PowerManagerError("Windows did not activate the requested power plan")
            state.applied = changed
            return PowerActionResult(changed, "Existing performance power plan selected", warnings)

        state.applied = False
        if state.modern_api_supported:
            if state.original_ac_mode_guid == BEST_PERFORMANCE_MODE_GUID:
                return PowerActionResult(
                    False,
                    "Windows AC power mode is already Best Performance",
                    warnings,
                )
            warnings.append(
                "No AC power change was prepared; start optimization again while connected to AC."
            )
            return PowerActionResult(False, "AC power mode unchanged", warnings)
        if state.original_plan_guid in {HIGH_PERFORMANCE_GUID, ULTIMATE_PERFORMANCE_GUID}:
            return PowerActionResult(
                False,
                "An existing performance power plan is already active",
                warnings,
            )
        warnings.append(
            "No existing High Performance power plan is available; the current plan was left unchanged."
        )
        return PowerActionResult(False, "Power plan unchanged", warnings)

    def restore(self, state: PowerModeState) -> PowerActionResult:
        """Restore exactly what this transaction prepared to change."""

        changed = False
        restored: list[str] = []

        # A target is a write-ahead marker.  Restore even when ``applied`` is
        # false because a crash can occur after Set* succeeds but before the
        # journal is updated.
        if state.target_ac_mode_guid and state.original_ac_mode_guid:
            original = self._normalized_guid(state.original_ac_mode_guid)
            api = self._native_api()
            if api is None:
                raise PowerModeApiUnavailable("Cannot restore the original AC power mode")
            current = self._normalized_guid(api.get_ac_mode())
            if current != original:
                api.set_ac_mode(original)
                changed = True
            if self._normalized_guid(api.get_ac_mode()) != original:
                raise PowerManagerError("Windows did not restore the original AC power mode")
            restored.append("AC power mode")

        if state.target_plan_guid and state.original_plan_guid:
            original_plan = self._normalized_guid(state.original_plan_guid)
            current, _ = current_power_plan()
            if self._normalized_guid(current) != original_plan:
                set_power_plan(original_plan)
                changed = True
            verified, _ = current_power_plan()
            if self._normalized_guid(verified) != original_plan:
                raise PowerManagerError("Windows did not restore the original power plan")
            restored.append("power plan")

        state.applied = False
        if not restored:
            return PowerActionResult(False, "No power setting required restoration")
        return PowerActionResult(changed, f"Restored original {' and '.join(restored)}")


# Both names are kept explicit: the service manages a modern power mode first
# and a legacy power plan only as a compatibility fallback.
PowerManager = PowerPlanManager
