"""Small, documented Windows session changes with exact journaled restore."""

from __future__ import annotations

import logging
from collections.abc import Callable

from sas_booster.models import StateSnapshot
from sas_booster.services.windows_settings import WindowsSettings
from sas_booster.utils.windows import is_admin

try:
    import winreg
except ImportError:  # pragma: no cover - non-Windows
    winreg = None  # type: ignore[assignment]


PersistCallback = Callable[[], None]
StatusCallback = Callable[[str], None]


class WindowsGamingOptimizer:
    """Apply only settings whose prior value can be restored precisely."""

    def __init__(self, settings: WindowsSettings) -> None:
        self.settings = settings
        self.log = logging.getLogger("sas_booster")

    def apply(
        self,
        snapshot: StateSnapshot,
        options: dict[str, object],
        persist: PersistCallback,
        status: StatusCallback,
    ) -> None:
        dword = int(winreg.REG_DWORD) if winreg is not None else 4
        string = int(winreg.REG_SZ) if winreg is not None else 1
        changes: list[tuple[bool, str, str, str, int | str, int]] = [
            (
                bool(options.get("enable_game_mode", True)),
                "Game Mode",
                "Software\\Microsoft\\GameBar",
                "AutoGameModeEnabled",
                1,
                dword,
            ),
            (
                bool(options.get("disable_xbox_recording", True)),
                "Xbox background capture",
                "Software\\Microsoft\\Windows\\CurrentVersion\\GameDVR",
                "AppCaptureEnabled",
                0,
                dword,
            ),
            (
                bool(options.get("disable_xbox_recording", True)),
                "Game DVR",
                "System\\GameConfigStore",
                "GameDVR_Enabled",
                0,
                dword,
            ),
            (
                bool(options.get("disable_transparency", False)),
                "Transparency effects",
                "Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize",
                "EnableTransparency",
                0,
                dword,
            ),
            (
                bool(options.get("disable_window_animation", False)),
                "Window animations",
                "Control Panel\\Desktop\\WindowMetrics",
                "MinAnimate",
                "0",
                string,
            ),
        ]
        for enabled, label, path, name, desired, value_type in changes:
            if enabled:
                self._apply_registry(snapshot, label, path, name, desired, value_type, persist, status)

        service_options = (
            ("pause_search", "WSearch", "Windows Search"),
            ("pause_sysmain", "SysMain", "SysMain"),
            ("pause_delivery_optimization", "DoSvc", "Delivery Optimization"),
        )
        for option, service_name, label in service_options:
            if not bool(options.get(option, False)):
                continue
            if not is_admin():
                message = f"{label} skipped: Administrator Mode is required"
                snapshot.warnings.append(message)
                persist()
                status(message)
                self.log.info("Action skipped | action=%s | reason=standard-mode", label)
                continue
            self._apply_service(snapshot, service_name, label, persist, status)

        if bool(options.get("disable_network_power_saving", False)):
            message = "Network power saving skipped: the active MT7902 driver does not expose a safe reversible API"
            snapshot.warnings.append(message)
            persist()
            status(message)
            self.log.info("Action skipped | action=network-power | reason=driver-unsupported")
        if bool(options.get("disable_usb_selective_suspend", False)):
            message = "USB selective suspend skipped: no supported setting is exposed by the active power plan"
            snapshot.warnings.append(message)
            persist()
            status(message)
            self.log.info("Action skipped | action=usb-selective-suspend | reason=setting-unavailable")
        if bool(options.get("reduce_amd_metrics_overlay", False)):
            message = "AMD Metrics Overlay requires a manual per-game change in Radeon Software; no undocumented database was edited"
            snapshot.warnings.append(message)
            persist()
            status(message)
            self.log.info("Action skipped | action=amd-metrics-overlay | reason=no-documented-api")

    def _apply_registry(
        self,
        snapshot: StateSnapshot,
        label: str,
        path: str,
        name: str,
        desired: int | str,
        value_type: int,
        persist: PersistCallback,
        status: StatusCallback,
    ) -> None:
        try:
            original = self.settings.read_registry_value("HKCU", path, name)
            if original.existed and original.value == desired and original.value_type == value_type:
                status(f"{label} already configured")
                self.log.info("Action skipped | action=%s | reason=already-configured", label)
                return
            original.applied = True  # prepared marker covers a crash after SetValue
            snapshot.registry_values.append(original)
            persist()  # write-ahead before the mutation
            self.settings.write_registry_value(original, desired, value_type)
            persist()
            status(f"{label} optimized for this session")
            self.log.info("Action applied | action=%s", label)
        except (OSError, PermissionError, TypeError, ValueError) as exc:
            message = f"{label} skipped: {type(exc).__name__}"
            snapshot.warnings.append(message)
            persist()
            status(message)
            self.log.warning("Action failed | action=%s | error=%s", label, type(exc).__name__)

    def _apply_service(
        self,
        snapshot: StateSnapshot,
        service_name: str,
        label: str,
        persist: PersistCallback,
        status: StatusCallback,
    ) -> None:
        try:
            original = self.settings.capture_service(service_name)
            if not original.was_running:
                status(f"{label} is already stopped")
                return
            original.applied = True  # prepared marker covers a partially completed stop
            snapshot.services.append(original)
            persist()
            self.settings.stop_service(original)
            persist()
            status(f"{label} paused for this session")
            self.log.info("Action applied | action=pause-service | service=%s", service_name)
        except (OSError, PermissionError, ValueError, RuntimeError) as exc:
            message = f"{label} skipped: {type(exc).__name__}"
            snapshot.warnings.append(message)
            persist()
            status(message)
            self.log.warning("Action failed | action=pause-service | service=%s | error=%s", service_name, type(exc).__name__)

    def restore(
        self,
        snapshot: StateSnapshot,
        persist: PersistCallback,
        errors: list[str],
    ) -> None:
        for service in reversed(snapshot.services):
            if not service.applied:
                continue
            try:
                self.settings.restore_service(service)
                service.applied = False
                persist()
                self.log.info("Action restored | action=service | service=%s", service.name)
            except Exception as exc:  # restore must continue through independent records
                errors.append(f"Restore service {service.name}: {type(exc).__name__}")
                self.log.error("Restore failed | action=service | service=%s | error=%s", service.name, type(exc).__name__)

        for registry in reversed(snapshot.registry_values):
            if not registry.applied:
                continue
            try:
                self.settings.restore_registry_value(registry)
                registry.applied = False
                persist()
                self.log.info("Action restored | action=registry | value=%s", registry.name)
            except Exception as exc:
                errors.append(f"Restore registry {registry.name}: {type(exc).__name__}")
                self.log.error("Restore failed | action=registry | value=%s | error=%s", registry.name, type(exc).__name__)
