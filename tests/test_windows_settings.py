from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "vendor"))
sys.path.insert(0, str(ROOT))

from sas_booster.models import ServiceState, StateSnapshot
from sas_booster.services.windows_gaming import WindowsGamingOptimizer
from sas_booster.services.windows_settings import SC_EXE, WindowsSettings
from sas_booster.utils.windows import CommandResult


class FakeService:
    """Small psutil service double; deliberately has no start/stop methods."""

    def __init__(self, statuses: list[str]) -> None:
        self._statuses = iter(statuses)
        self._last = statuses[-1]

    def status(self) -> str:
        try:
            self._last = next(self._statuses)
        except StopIteration:
            pass
        return self._last


class WindowsServiceControlTests(unittest.TestCase):
    def test_optimizer_service_round_trip_uses_write_ahead_and_restores(self) -> None:
        service = FakeService(
            [
                "running",  # capture_service
                "running",  # stop_service
                "stop_pending",
                "stopped",
                "stopped",  # restore_service
                "start_pending",
                "running",
            ]
        )
        snapshot = StateSnapshot(session_id="service-round-trip")
        persisted: list[list[tuple[str, bool]]] = []

        def persist() -> None:
            persisted.append([(item.name, item.applied) for item in snapshot.services])

        def command_result(_args: list[str], *, timeout: float) -> CommandResult:
            self.assertEqual(timeout, 15.0)
            self.assertEqual(persisted[-1], [("WSearch", True)])
            return CommandResult(0, "", "")

        options = {
            "enable_game_mode": False,
            "disable_xbox_recording": False,
            "disable_transparency": False,
            "disable_window_animation": False,
            "pause_search": True,
        }
        transaction = WindowsGamingOptimizer(WindowsSettings())
        errors: list[str] = []

        with (
            patch("sas_booster.services.windows_gaming.is_admin", return_value=True),
            patch("sas_booster.services.windows_settings.is_admin", return_value=True),
            patch("sas_booster.services.windows_settings.is_windows", return_value=True),
            patch("sas_booster.services.windows_settings.psutil.win_service_get", return_value=service),
            patch(
                "sas_booster.services.windows_settings.run_command",
                side_effect=command_result,
            ) as command,
            patch("sas_booster.services.windows_settings.time.sleep"),
        ):
            transaction.apply(snapshot, options, persist, lambda _message: None)
            self.assertTrue(snapshot.services[0].applied)
            transaction.restore(snapshot, persist, errors)

        self.assertEqual(errors, [])
        self.assertFalse(snapshot.services[0].applied)
        self.assertEqual(
            [call.args[0][1:] for call in command.call_args_list],
            [["stop", "WSearch"], ["start", "WSearch"]],
        )

    def test_stop_uses_sc_and_waits_for_stopped_state(self) -> None:
        service = FakeService(["running", "stop_pending", "stopped"])
        state = ServiceState(name="WSearch", was_running=True, applied=True)

        with (
            patch("sas_booster.services.windows_settings.is_admin", return_value=True),
            patch("sas_booster.services.windows_settings.psutil.win_service_get", return_value=service),
            patch(
                "sas_booster.services.windows_settings.run_command",
                return_value=CommandResult(0, "", ""),
            ) as command,
            patch("sas_booster.services.windows_settings.time.sleep"),
        ):
            WindowsSettings.stop_service(state)

        command.assert_called_once_with([SC_EXE, "stop", "WSearch"], timeout=15.0)

    def test_restore_uses_sc_and_waits_for_running_state(self) -> None:
        service = FakeService(["stopped", "start_pending", "running"])
        state = ServiceState(name="SysMain", was_running=True, applied=True)

        with (
            patch("sas_booster.services.windows_settings.is_admin", return_value=True),
            patch("sas_booster.services.windows_settings.psutil.win_service_get", return_value=service),
            patch(
                "sas_booster.services.windows_settings.run_command",
                return_value=CommandResult(0, "", ""),
            ) as command,
            patch("sas_booster.services.windows_settings.time.sleep"),
        ):
            WindowsSettings.restore_service(state)

        command.assert_called_once_with([SC_EXE, "start", "SysMain"], timeout=15.0)

    def test_restore_is_idempotent_when_service_is_already_running(self) -> None:
        state = ServiceState(name="WSearch", was_running=True, applied=True)

        with (
            patch("sas_booster.services.windows_settings.is_admin", return_value=True),
            patch(
                "sas_booster.services.windows_settings.psutil.win_service_get",
                return_value=FakeService(["running"]),
            ),
            patch("sas_booster.services.windows_settings.run_command") as command,
        ):
            WindowsSettings.restore_service(state)

        command.assert_not_called()

    def test_existing_pending_stop_is_awaited_without_second_command(self) -> None:
        state = ServiceState(name="WSearch", was_running=True, applied=True)

        with (
            patch("sas_booster.services.windows_settings.is_admin", return_value=True),
            patch(
                "sas_booster.services.windows_settings.psutil.win_service_get",
                return_value=FakeService(["stop_pending", "stopped"]),
            ),
            patch("sas_booster.services.windows_settings.run_command") as command,
            patch("sas_booster.services.windows_settings.time.sleep"),
        ):
            WindowsSettings.stop_service(state)

        command.assert_not_called()

    def test_nonzero_command_is_accepted_when_stop_is_pending(self) -> None:
        state = ServiceState(name="WSearch", was_running=True, applied=True)

        with (
            patch("sas_booster.services.windows_settings.is_admin", return_value=True),
            patch(
                "sas_booster.services.windows_settings.psutil.win_service_get",
                return_value=FakeService(["running", "stop_pending", "stopped"]),
            ),
            patch(
                "sas_booster.services.windows_settings.run_command",
                return_value=CommandResult(1061, "Service control is pending", ""),
            ),
            patch("sas_booster.services.windows_settings.time.sleep"),
        ):
            WindowsSettings.stop_service(state)

    def test_service_command_failure_is_reported(self) -> None:
        state = ServiceState(name="WSearch", was_running=True, applied=True)

        with (
            patch("sas_booster.services.windows_settings.is_admin", return_value=True),
            patch(
                "sas_booster.services.windows_settings.psutil.win_service_get",
                return_value=FakeService(["running", "running"]),
            ),
            patch(
                "sas_booster.services.windows_settings.run_command",
                return_value=CommandResult(5, "Access is denied", ""),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "Access is denied"):
                WindowsSettings.stop_service(state)


if __name__ == "__main__":
    unittest.main()
