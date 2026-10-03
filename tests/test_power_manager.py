from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "vendor"))
sys.path.insert(0, str(ROOT))

from sas_booster.constants import HIGH_PERFORMANCE_GUID
from sas_booster.models import PowerModeState, RegistryValueState, ServiceState, StateSnapshot
from sas_booster.services.power_manager import (
    BEST_EFFICIENCY_MODE_GUID,
    BEST_PERFORMANCE_MODE_GUID,
    PowerManagerError,
    PowerModeApiUnavailable,
    PowerPlanManager,
    _GUID,
    _NativePowerModeApi,
)
from sas_booster.services.windows_gaming import WindowsGamingOptimizer
from sas_booster.utils.windows import PowerStatus


BALANCED_PLAN_GUID = "381b4222-f694-41f0-9685-ff5bb260df2e"


class FakePowerModeApi:
    def __init__(
        self,
        ac_mode: str = BEST_EFFICIENCY_MODE_GUID,
        dc_mode: str = BEST_EFFICIENCY_MODE_GUID,
    ) -> None:
        self.ac_mode = ac_mode
        self.dc_mode = dc_mode
        self.set_ac_calls: list[str] = []

    def get_ac_mode(self) -> str:
        return self.ac_mode

    def get_dc_mode(self) -> str:
        return self.dc_mode

    def set_ac_mode(self, guid: str) -> None:
        self.set_ac_calls.append(guid)
        self.ac_mode = guid


class UnsupportedPowerModeApi(FakePowerModeApi):
    def get_ac_mode(self) -> str:
        raise PowerModeApiUnavailable("not exported")


class FailedPowerModeApi(FakePowerModeApi):
    def get_ac_mode(self) -> str:
        raise PowerManagerError("access denied")


class PowerManagerTests(unittest.TestCase):
    def test_guid_native_layout_round_trips(self) -> None:
        self.assertEqual(
            _GUID.from_text(BEST_PERFORMANCE_MODE_GUID).to_text(),
            BEST_PERFORMANCE_MODE_GUID,
        )

    def test_snapshot_preserves_exact_ac_dc_and_prepares_ac_only(self) -> None:
        api = FakePowerModeApi()
        manager = PowerPlanManager(api)
        with (
            patch(
                "sas_booster.services.power_manager.system_power_status",
                return_value=PowerStatus(True, 42, False),
            ),
            patch(
                "sas_booster.services.power_manager.current_power_plan",
                return_value=(BALANCED_PLAN_GUID, "Balanced"),
            ),
        ):
            state = manager.snapshot()

        self.assertEqual(state.original_ac_mode_guid, BEST_EFFICIENCY_MODE_GUID)
        self.assertEqual(state.original_dc_mode_guid, BEST_EFFICIENCY_MODE_GUID)
        self.assertEqual(state.original_plan_guid, BALANCED_PLAN_GUID)
        self.assertEqual(state.target_ac_mode_guid, BEST_PERFORMANCE_MODE_GUID)
        self.assertIsNone(state.target_plan_guid)
        self.assertEqual(api.set_ac_calls, [])

    def test_apply_and_restore_change_ac_but_never_dc(self) -> None:
        api = FakePowerModeApi()
        manager = PowerPlanManager(api)
        with (
            patch(
                "sas_booster.services.power_manager.system_power_status",
                return_value=PowerStatus(True, 50, False),
            ),
            patch(
                "sas_booster.services.power_manager.current_power_plan",
                return_value=(BALANCED_PLAN_GUID, "Balanced"),
            ),
        ):
            state = manager.snapshot()
            applied = manager.apply(state)
            restored = manager.restore(state)

        self.assertTrue(applied.changed)
        self.assertTrue(restored.changed)
        self.assertEqual(
            api.set_ac_calls,
            [BEST_PERFORMANCE_MODE_GUID, BEST_EFFICIENCY_MODE_GUID],
        )
        self.assertEqual(api.ac_mode, BEST_EFFICIENCY_MODE_GUID)
        self.assertEqual(api.dc_mode, BEST_EFFICIENCY_MODE_GUID)
        self.assertFalse(state.applied)

    def test_battery_mode_warns_and_does_not_set_ac_or_dc(self) -> None:
        api = FakePowerModeApi()
        manager = PowerPlanManager(api)
        with (
            patch(
                "sas_booster.services.power_manager.system_power_status",
                return_value=PowerStatus(False, 25, True),
            ),
            patch(
                "sas_booster.services.power_manager.current_power_plan",
                return_value=(BALANCED_PLAN_GUID, "Balanced"),
            ),
        ):
            state = manager.snapshot()
            result = manager.apply(state)

        self.assertIsNone(state.target_ac_mode_guid)
        self.assertIsNone(state.target_plan_guid)
        self.assertFalse(result.changed)
        self.assertTrue(any("battery" in warning.lower() for warning in result.warnings))
        self.assertTrue(any("battery saver" in warning.lower() for warning in result.warnings))
        self.assertEqual(api.set_ac_calls, [])
        self.assertEqual(api.dc_mode, BEST_EFFICIENCY_MODE_GUID)

    def test_power_source_change_to_battery_aborts_prepared_ac_change(self) -> None:
        api = FakePowerModeApi()
        manager = PowerPlanManager(api)
        with (
            patch(
                "sas_booster.services.power_manager.system_power_status",
                return_value=PowerStatus(True, 60, False),
            ),
            patch(
                "sas_booster.services.power_manager.current_power_plan",
                return_value=(BALANCED_PLAN_GUID, "Balanced"),
            ),
        ):
            state = manager.snapshot()

        with patch(
            "sas_booster.services.power_manager.system_power_status",
            return_value=PowerStatus(False, 59, False),
        ):
            result = manager.apply(state)

        self.assertFalse(result.changed)
        self.assertEqual(api.set_ac_calls, [])

    def test_restore_uses_write_ahead_target_even_when_applied_is_false(self) -> None:
        api = FakePowerModeApi(ac_mode=BEST_PERFORMANCE_MODE_GUID)
        manager = PowerPlanManager(api)
        state = PowerModeState(
            original_ac_mode_guid=BEST_EFFICIENCY_MODE_GUID,
            original_dc_mode_guid=BEST_EFFICIENCY_MODE_GUID,
            original_plan_guid=BALANCED_PLAN_GUID,
            target_ac_mode_guid=BEST_PERFORMANCE_MODE_GUID,
            modern_api_supported=True,
            applied=False,
        )

        result = manager.restore(state)

        self.assertTrue(result.changed)
        self.assertEqual(api.set_ac_calls, [BEST_EFFICIENCY_MODE_GUID])
        self.assertEqual(api.dc_mode, BEST_EFFICIENCY_MODE_GUID)

    def test_unsupported_modern_api_uses_only_an_existing_plan(self) -> None:
        manager = PowerPlanManager(UnsupportedPowerModeApi())
        with (
            patch(
                "sas_booster.services.power_manager.system_power_status",
                return_value=PowerStatus(True, 80, False),
            ),
            patch(
                "sas_booster.services.power_manager.current_power_plan",
                return_value=(BALANCED_PLAN_GUID, "Balanced"),
            ),
            patch(
                "sas_booster.services.power_manager.available_power_plans",
                return_value={HIGH_PERFORMANCE_GUID: "High performance"},
            ),
        ):
            state = manager.snapshot()

        self.assertFalse(state.modern_api_supported)
        self.assertEqual(state.target_plan_guid, HIGH_PERFORMANCE_GUID)
        self.assertIsNone(state.target_ac_mode_guid)

        with (
            patch(
                "sas_booster.services.power_manager.system_power_status",
                return_value=PowerStatus(True, 80, False),
            ),
            patch(
                "sas_booster.services.power_manager.available_power_plans",
                return_value={HIGH_PERFORMANCE_GUID: "High performance"},
            ),
            patch(
                "sas_booster.services.power_manager.current_power_plan",
                side_effect=[
                    (BALANCED_PLAN_GUID, "Balanced"),
                    (HIGH_PERFORMANCE_GUID, "High performance"),
                ],
            ),
            patch("sas_booster.services.power_manager.set_power_plan") as set_plan,
        ):
            result = manager.apply(state)

        self.assertTrue(result.changed)
        set_plan.assert_called_once_with(HIGH_PERFORMANCE_GUID)

        with (
            patch(
                "sas_booster.services.power_manager.current_power_plan",
                side_effect=[
                    (HIGH_PERFORMANCE_GUID, "High performance"),
                    (BALANCED_PLAN_GUID, "Balanced"),
                ],
            ),
            patch("sas_booster.services.power_manager.set_power_plan") as set_plan,
        ):
            restored = manager.restore(state)

        self.assertTrue(restored.changed)
        set_plan.assert_called_once_with(BALANCED_PLAN_GUID)

    def test_supported_api_failure_never_falls_back_to_a_plan(self) -> None:
        manager = PowerPlanManager(FailedPowerModeApi())
        with (
            patch(
                "sas_booster.services.power_manager.system_power_status",
                return_value=PowerStatus(True, 75, False),
            ),
            patch(
                "sas_booster.services.power_manager.current_power_plan",
                return_value=(BALANCED_PLAN_GUID, "Balanced"),
            ),
            patch("sas_booster.services.power_manager.available_power_plans") as plans,
        ):
            with self.assertRaises(PowerManagerError):
                manager.snapshot()

        plans.assert_not_called()

    def test_legacy_fallback_refuses_change_when_original_plan_is_unknown(self) -> None:
        manager = PowerPlanManager(UnsupportedPowerModeApi())
        with (
            patch(
                "sas_booster.services.power_manager.system_power_status",
                return_value=PowerStatus(True, 75, False),
            ),
            patch(
                "sas_booster.services.power_manager.current_power_plan",
                return_value=(None, "Unknown"),
            ),
            patch("sas_booster.services.power_manager.available_power_plans") as plans,
        ):
            state = manager.snapshot()

        self.assertIsNone(state.original_plan_guid)
        self.assertIsNone(state.target_plan_guid)
        plans.assert_not_called()

    def test_only_documented_unsupported_errors_enable_fallback(self) -> None:
        for code in (50, 120, 127):
            with self.subTest(code=code):
                with self.assertRaises(PowerModeApiUnavailable):
                    _NativePowerModeApi._check(code, "test")
        with self.assertRaises(PowerManagerError):
            _NativePowerModeApi._check(5, "test")

    def test_power_mode_state_round_trips_through_session_json_model(self) -> None:
        power = PowerModeState(
            original_ac_mode_guid=BEST_EFFICIENCY_MODE_GUID,
            original_dc_mode_guid=BEST_EFFICIENCY_MODE_GUID,
            original_plan_guid=BALANCED_PLAN_GUID,
            target_ac_mode_guid=BEST_PERFORMANCE_MODE_GUID,
            on_ac_power=True,
            modern_api_supported=True,
        )
        restored = StateSnapshot.from_dict(
            StateSnapshot(session_id="power-test", power_mode=power).to_dict()
        )
        self.assertIsNotNone(restored.power_mode)
        self.assertEqual(restored.power_mode.original_ac_mode_guid, BEST_EFFICIENCY_MODE_GUID)
        self.assertEqual(restored.power_mode.original_dc_mode_guid, BEST_EFFICIENCY_MODE_GUID)


class FakeWindowsGamingSettings:
    def __init__(self) -> None:
        self.restored_services: list[ServiceState] = []
        self.restored_registry: list[RegistryValueState] = []

    def restore_service(self, state: ServiceState) -> None:
        self.restored_services.append(state)

    def restore_registry_value(self, state: RegistryValueState) -> None:
        self.restored_registry.append(state)


class WriteAheadWindowsGamingSettings(FakeWindowsGamingSettings):
    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self.events = events

    def read_registry_value(self, hive: str, path: str, name: str) -> RegistryValueState:
        return RegistryValueState(hive=hive, path=path, name=name, existed=False)

    def write_registry_value(
        self,
        original: RegistryValueState,
        value: int | str,
        value_type: int,
    ) -> None:
        self.events.append("mutation")


class WindowsGamingWriteAheadRestoreTests(unittest.TestCase):
    def test_registry_marker_is_persisted_before_windows_mutation(self) -> None:
        events: list[str] = []
        settings = WriteAheadWindowsGamingSettings(events)
        optimizer = WindowsGamingOptimizer(settings)  # type: ignore[arg-type]
        snapshot = StateSnapshot(session_id="write-ahead-order")

        def persist() -> None:
            self.assertTrue(snapshot.registry_values)
            self.assertTrue(snapshot.registry_values[-1].applied)
            events.append("persist")

        optimizer.apply(
            snapshot,
            {
                "enable_game_mode": True,
                "disable_xbox_recording": False,
                "disable_transparency": False,
                "disable_window_animation": False,
            },
            persist,
            lambda _message: None,
        )

        self.assertEqual(events, ["persist", "mutation", "persist"])

    def test_prepared_records_are_restored_and_completed_records_are_skipped(self) -> None:
        settings = FakeWindowsGamingSettings()
        optimizer = WindowsGamingOptimizer(settings)  # type: ignore[arg-type]
        service = ServiceState(name="WSearch", was_running=True, applied=True)
        completed_service = ServiceState(name="SysMain", was_running=True, applied=False)
        registry = RegistryValueState(
            hive="HKCU",
            path=r"Software\Microsoft\GameBar",
            name="AutoGameModeEnabled",
            existed=False,
            applied=True,
        )
        completed_registry = RegistryValueState(
            hive="HKCU",
            path=r"Software\Microsoft\GameBar",
            name="CompletedValue",
            existed=False,
            applied=False,
        )
        snapshot = StateSnapshot(
            session_id="write-ahead",
            services=[completed_service, service],
            registry_values=[completed_registry, registry],
        )
        persist_calls: list[bool] = []
        errors: list[str] = []

        optimizer.restore(snapshot, lambda: persist_calls.append(True), errors)

        self.assertEqual(errors, [])
        self.assertEqual(settings.restored_services, [service])
        self.assertEqual(settings.restored_registry, [registry])
        self.assertFalse(service.applied)
        self.assertFalse(registry.applied)
        self.assertEqual(len(persist_calls), 2)


if __name__ == "__main__":
    unittest.main()
