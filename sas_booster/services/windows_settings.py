"""Documented, reversible Windows settings used during one game session."""

from __future__ import annotations

from contextlib import suppress
import os
import time

import psutil

from sas_booster.constants import (
    ALLOWED_SESSION_SERVICES,
    HIGH_PERFORMANCE_GUID,
    PROTECTED_SERVICE_NAMES,
    ULTIMATE_PERFORMANCE_GUID,
)
from sas_booster.models import RegistryValueState, ServiceState
from sas_booster.utils.windows import (
    available_power_plans,
    current_power_plan,
    is_admin,
    is_windows,
    run_command,
    set_power_plan,
)

try:
    import winreg
except ImportError:  # pragma: no cover
    winreg = None  # type: ignore[assignment]


GAME_MODE_KEY = r"Software\Microsoft\GameBar"
GAME_MODE_VALUE = "AutoGameModeEnabled"
SERVICE_COMMAND_TIMEOUT = 15.0
SERVICE_TRANSITION_TIMEOUT = 30.0
SERVICE_POLL_INTERVAL = 0.2
SC_EXE = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "sc.exe")


def _service_status(name: str) -> str:
    return psutil.win_service_get(name).status().strip().lower().replace(" ", "_")


def _wait_for_service_status(name: str, expected: str) -> None:
    deadline = time.monotonic() + SERVICE_TRANSITION_TIMEOUT
    while True:
        status = _service_status(name)
        if status == expected:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"Service {name} did not reach the {expected} state")
        time.sleep(min(SERVICE_POLL_INTERVAL, remaining))


def _control_service(name: str, action: str, expected: str, pending: str) -> None:
    result = run_command([SC_EXE, action, name], timeout=SERVICE_COMMAND_TIMEOUT)
    status = _service_status(name)
    if status == expected:
        return
    if result.returncode and status != pending:
        detail = result.stderr or result.stdout or f"sc.exe exited with code {result.returncode}"
        raise RuntimeError(f"Unable to {action} service {name}: {detail}")
    _wait_for_service_status(name, expected)


class WindowsSettings:
    @staticmethod
    def _hive(name: str):
        if winreg is None:
            raise OSError("Windows registry is unavailable")
        mapping = {
            "HKCU": winreg.HKEY_CURRENT_USER,
            "HKLM": winreg.HKEY_LOCAL_MACHINE,
        }
        try:
            return mapping[name]
        except KeyError as exc:
            raise ValueError(f"Unsupported registry hive {name}") from exc

    def read_registry_value(self, hive: str, path: str, name: str) -> RegistryValueState:
        if not is_windows() or winreg is None:
            raise OSError("Registry settings are only available on Windows")
        root = self._hive(hive)
        try:
            with winreg.OpenKey(root, path, 0, winreg.KEY_READ) as key:
                try:
                    value, value_type = winreg.QueryValueEx(key, name)
                    if not isinstance(value, (int, str)):
                        raise TypeError(f"Unsupported registry type for {name}")
                    return RegistryValueState(hive, path, name, True, value, int(value_type))
                except FileNotFoundError:
                    return RegistryValueState(hive, path, name, False)
        except FileNotFoundError:
            return RegistryValueState(hive, path, name, False)

    def write_registry_value(self, state: RegistryValueState, value: int | str, value_type: int) -> None:
        if not is_windows() or winreg is None:
            raise OSError("Registry settings are only available on Windows")
        access = winreg.KEY_SET_VALUE
        with winreg.CreateKeyEx(self._hive(state.hive), state.path, 0, access) as key:
            winreg.SetValueEx(key, state.name, 0, value_type, value)

    def restore_registry_value(self, state: RegistryValueState) -> None:
        if not is_windows() or winreg is None:
            return
        with winreg.CreateKeyEx(self._hive(state.hive), state.path, 0, winreg.KEY_SET_VALUE) as key:
            if state.existed and state.value_type is not None and state.value is not None:
                winreg.SetValueEx(key, state.name, 0, state.value_type, state.value)
            else:
                with suppress(FileNotFoundError):
                    winreg.DeleteValue(key, state.name)

    def read_game_mode(self) -> tuple[int | None, bool]:
        state = self.read_registry_value("HKCU", GAME_MODE_KEY, GAME_MODE_VALUE)
        return (int(state.value), True) if state.existed and isinstance(state.value, int) else (None, False)

    def set_game_mode(self, enabled: bool) -> None:
        state = RegistryValueState("HKCU", GAME_MODE_KEY, GAME_MODE_VALUE, False)
        self.write_registry_value(state, int(enabled), winreg.REG_DWORD if winreg else 4)

    def restore_game_mode(self, value: int | None, existed: bool) -> None:
        state = RegistryValueState(
            "HKCU",
            GAME_MODE_KEY,
            GAME_MODE_VALUE,
            existed,
            value,
            winreg.REG_DWORD if winreg else 4,
        )
        self.restore_registry_value(state)

    @staticmethod
    def choose_performance_plan() -> str | None:
        plans = available_power_plans()
        for guid in (ULTIMATE_PERFORMANCE_GUID, HIGH_PERFORMANCE_GUID):
            if guid in plans:
                set_power_plan(guid)
                return guid
        return None

    @staticmethod
    def power_plan() -> tuple[str | None, str]:
        return current_power_plan()

    @staticmethod
    def capture_service(name: str) -> ServiceState:
        if name not in ALLOWED_SESSION_SERVICES or name in PROTECTED_SERVICE_NAMES:
            raise PermissionError(f"Service {name} is not approved for session control")
        if not is_windows():
            raise OSError("Windows services are unavailable")
        service = psutil.win_service_get(name)
        return ServiceState(name=name, was_running=service.status().lower() == "running")

    @staticmethod
    def stop_service(state: ServiceState) -> None:
        if not is_admin():
            raise PermissionError("Administrator privileges are required for service control")
        if state.name not in ALLOWED_SESSION_SERVICES or state.name in PROTECTED_SERVICE_NAMES:
            raise PermissionError(f"Service {state.name} is not approved")
        if not state.was_running:
            return
        status = _service_status(state.name)
        if status == "stopped":
            return
        if status == "stop_pending":
            _wait_for_service_status(state.name, "stopped")
            return
        _control_service(state.name, "stop", "stopped", "stop_pending")

    @staticmethod
    def restore_service(state: ServiceState) -> None:
        if not state.applied or not state.was_running:
            return
        if not is_admin():
            raise PermissionError("Administrator privileges are required for service control")
        if state.name not in ALLOWED_SESSION_SERVICES or state.name in PROTECTED_SERVICE_NAMES:
            raise PermissionError(f"Service {state.name} is not approved")
        status = _service_status(state.name)
        if status == "running":
            return
        if status == "start_pending":
            _wait_for_service_status(state.name, "running")
            return
        _control_service(state.name, "start", "running", "start_pending")

    # Legacy v2 journal support.
    @classmethod
    def resume_service(cls, name: str) -> None:
        cls.restore_service(ServiceState(name=name, was_running=True, applied=True))
