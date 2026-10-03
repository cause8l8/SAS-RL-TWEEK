from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "vendor"))
sys.path.insert(0, str(ROOT))

from sas_booster.config import ConfigStore
from sas_booster.models import (
    GameProfile,
    PowerModeState,
    ProcessIdentity,
    RegistryValueState,
    StateSnapshot,
)
from sas_booster.services.optimizer import GameLaunchError, OptimizerService, RestoreIncomplete
from sas_booster.services.game_process_watcher import GameWatchResult, WatchStopReason
from sas_booster.services.power_manager import PowerActionResult
from sas_booster.services.processes import ProcessService
from sas_booster.services.state_store import CorruptStateError, StateStore


class FakeSettings:
    def __init__(self) -> None:
        self.fail_registry_restore = False
        self.registry_restore_calls: list[str] = []

    def restore_registry_value(self, state: RegistryValueState) -> None:
        self.registry_restore_calls.append(state.name)
        if self.fail_registry_restore:
            raise PermissionError("simulated registry restore failure")

    def restore_service(self, _state: object) -> None:
        return None

    def resume_service(self, _name: str) -> None:
        return None

    def restore_game_mode(self, _value: int | None, _existed: bool) -> None:
        return None


class FakePowerManager:
    def __init__(self) -> None:
        self.snapshot_calls = 0
        self.apply_calls = 0
        self.restore_calls = 0

    def snapshot(self) -> PowerModeState:
        self.snapshot_calls += 1
        return PowerModeState(
            original_ac_mode_guid="961cc777-2547-4f9d-8174-7d86181b8a7a",
            original_dc_mode_guid="961cc777-2547-4f9d-8174-7d86181b8a7a",
            target_ac_mode_guid="ded574b5-45a0-4f42-8737-46345c09c238",
            on_ac_power=True,
            modern_api_supported=True,
        )

    def apply(self, state: PowerModeState) -> PowerActionResult:
        self.apply_calls += 1
        state.applied = True
        return PowerActionResult(True, "fake AC power mode applied")

    def restore(self, state: PowerModeState) -> PowerActionResult:
        self.restore_calls += 1
        state.applied = False
        return PowerActionResult(True, "fake AC power mode restored")


class FakeNetworkOptimizer:
    def apply(self, *_args: object, **_kwargs: object) -> None:
        return None


class FakeGameConfigManager:
    def restore_session_values(self, *_args: object, **_kwargs: object) -> None:
        return None


class FakeWindowsTransaction:
    """Simulate one journaled Windows mutation without touching Windows."""

    def __init__(self) -> None:
        self.apply_calls = 0
        self.restore_calls = 0
        self.mutated = False

    def apply(self, snapshot, _options, persist, _status) -> None:
        self.apply_calls += 1
        marker = RegistryValueState(
            hive="HKCU",
            path=r"Software\SAS\Tests",
            name="LaunchTransaction",
            existed=False,
            applied=True,
        )
        snapshot.registry_values.append(marker)
        persist()
        self.mutated = True

    def restore(self, snapshot, persist, _errors) -> None:
        for marker in snapshot.registry_values:
            if not marker.applied:
                continue
            self.restore_calls += 1
            self.mutated = False
            marker.applied = False
            persist()


class FakeRuntimeProcess:
    def __init__(self, name: str, create_time: float) -> None:
        self._name = name
        self._create_time = create_time
        self.nice_calls: list[int] = []

    def name(self) -> str:
        return self._name

    def create_time(self) -> float:
        return self._create_time

    def nice(self, value: int) -> None:
        self.nice_calls.append(value)


class FakeDetectedGameProcess:
    def __init__(self, pid: int, created: float, executable: Path) -> None:
        self.pid = pid
        self._created = created
        self._executable = str(executable)
        self.info = {
            "pid": pid,
            "name": "RocketLeague.exe",
            "create_time": created,
            "exe": self._executable,
        }

    def name(self) -> str:
        return "RocketLeague.exe"

    def create_time(self) -> float:
        return self._created

    def exe(self) -> str:
        return self._executable

    def nice(self) -> int:
        return 32

    def cpu_affinity(self) -> list[int]:
        return [0, 1, 2, 3]

    def cmdline(self) -> list[str]:
        return [self._executable]


class RestoreTransactionTests(unittest.TestCase):
    EPIC_LAUNCH_URI = "com.epicgames.launcher://apps/Sugar?action=launch&silent=true"

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = StateStore(self.root / "state")
        self.settings = FakeSettings()
        self.power = FakePowerManager()
        self.optimizer = OptimizerService(
            ConfigStore(self.root / "config.json"),
            self.store,
            self.settings,  # type: ignore[arg-type]
            ProcessService(),
            power=self.power,  # type: ignore[arg-type]
            game_config=FakeGameConfigManager(),  # type: ignore[arg-type]
            network=FakeNetworkOptimizer(),  # type: ignore[arg-type]
        )
        self.mutex_patch = patch(
            "sas_booster.services.optimizer.SessionMutex",
            side_effect=lambda: nullcontext(),
        )
        self.mutex_patch.start()

    def tearDown(self) -> None:
        self.mutex_patch.stop()
        self.temporary.cleanup()

    def _registry_snapshot(self, session_id: str = "restore-test") -> StateSnapshot:
        return StateSnapshot(
            session_id=session_id,
            registry_values=[
                RegistryValueState(
                    hive="HKCU",
                    path=r"Software\SAS\Tests",
                    name="ExampleValue",
                    existed=False,
                    applied=True,
                )
            ],
        )

    def _installation(self, *, create_files: bool) -> GameProfile:
        install_root = self.root / "Rocket League"
        launch_executable = install_root / "Launcher.exe"
        process_executable = install_root / "Binaries" / "Win64" / "RocketLeague.exe"
        if create_files:
            launch_executable.parent.mkdir(parents=True, exist_ok=True)
            process_executable.parent.mkdir(parents=True, exist_ok=True)
            launch_executable.touch()
            process_executable.touch()
        return GameProfile(
            id="test-game", name="Test Game", platform="Epic Games",
            install_root=str(install_root), launch_executable=str(launch_executable),
            executable=str(process_executable),
        )

    def test_restore_is_idempotent_after_journal_is_archived(self) -> None:
        snapshot = self._registry_snapshot("idempotent")
        self.store.save(snapshot)
        statuses: list[str] = []

        restored = self.optimizer.restore(statuses.append)
        second = self.optimizer.restore(statuses.append)

        self.assertIsNotNone(restored)
        self.assertFalse(restored.active)
        self.assertEqual(restored.phase, "RESTORED")
        self.assertFalse(restored.registry_values[0].applied)
        self.assertIsNone(second)
        self.assertEqual(self.settings.registry_restore_calls, ["ExampleValue"])
        self.assertFalse(self.store.active_path.exists())
        self.assertTrue((self.store.directory / "session_idempotent.json").is_file())
        self.assertEqual(statuses[-1], "No active session to restore")

    def test_incomplete_restore_keeps_journal_and_can_retry(self) -> None:
        snapshot = self._registry_snapshot("retry")
        self.store.save(snapshot)
        self.settings.fail_registry_restore = True

        with self.assertRaises(RestoreIncomplete):
            self.optimizer.restore(lambda _message: None)

        remaining = self.store.load_active()
        self.assertIsNotNone(remaining)
        self.assertTrue(remaining.active)
        self.assertEqual(remaining.phase, "RESTORE_INCOMPLETE")
        self.assertTrue(remaining.registry_values[0].applied)
        self.assertTrue(remaining.errors)

        self.settings.fail_registry_restore = False
        restored = self.optimizer.restore(lambda _message: None)

        self.assertIsNotNone(restored)
        self.assertFalse(restored.active)
        self.assertEqual(
            self.settings.registry_restore_calls,
            ["ExampleValue", "ExampleValue"],
        )
        self.assertFalse(self.store.active_path.exists())

    def test_corrupt_journal_blocks_restore_and_new_optimization(self) -> None:
        self.store.active_path.parent.mkdir(parents=True, exist_ok=True)
        self.store.active_path.write_text('{"session_id":', encoding="utf-8")
        original = self.store.active_path.read_bytes()

        with self.assertRaises(CorruptStateError):
            self.optimizer.restore(lambda _message: None)

        with (
            patch.object(self.optimizer, "_launch") as launch,
            self.assertRaises(CorruptStateError),
        ):
            self.optimizer.optimize_and_launch(
                self._installation(create_files=False),
                lambda _message: None,
            )

        launch.assert_not_called()
        self.assertEqual(self.store.active_path.read_bytes(), original)
        self.assertEqual(self.settings.registry_restore_calls, [])

    def test_expected_session_id_mismatch_does_not_touch_journal(self) -> None:
        self.store.save(self._registry_snapshot("actual-session"))
        original = self.store.active_path.read_bytes()

        with self.assertRaisesRegex(RuntimeError, "identifier does not match"):
            self.optimizer.restore(
                lambda _message: None,
                expected_session_id="different-session",
            )

        self.assertEqual(self.store.active_path.read_bytes(), original)
        remaining = self.store.load_active()
        self.assertIsNotNone(remaining)
        self.assertEqual(remaining.phase, "PREPARED")
        self.assertEqual(self.settings.registry_restore_calls, [])

    def test_process_restore_requires_exact_pid_name_and_creation_time(self) -> None:
        exact_identity = ProcessIdentity(
            pid=101,
            name="browser.exe",
            create_time=10.0,
            priority=8,
            action="lower_priority",
            applied=True,
        )
        reused_identity = ProcessIdentity(
            pid=202,
            name="browser.exe",
            create_time=20.0,
            priority=9,
            action="lower_priority",
            applied=True,
        )
        exact_process = FakeRuntimeProcess("browser.exe", 10.0)
        reused_process = FakeRuntimeProcess("browser.exe", 999.0)
        self.store.save(
            StateSnapshot(
                session_id="process-identity",
                processes=[exact_identity, reused_identity],
            )
        )

        with patch(
            "sas_booster.services.processes.psutil.Process",
            side_effect=lambda pid: exact_process if pid == 101 else reused_process,
        ):
            restored = self.optimizer.restore(lambda _message: None)

        self.assertIsNotNone(restored)
        self.assertEqual(exact_process.nice_calls, [8])
        self.assertEqual(reused_process.nice_calls, [])
        self.assertTrue(all(not identity.applied for identity in restored.processes))

    def test_launch_failure_runs_finally_restore_and_archives_session(self) -> None:
        installation = self._installation(create_files=True)
        windows = FakeWindowsTransaction()
        self.optimizer.windows = windows  # type: ignore[assignment]
        statuses: list[str] = []

        with (
            patch.object(self.optimizer, "_matching_game_is_running", return_value=False),
            patch.object(
                self.optimizer,
                "_launch",
                side_effect=OSError("simulated launcher failure"),
            ),
            self.assertRaisesRegex(OSError, "simulated launcher failure"),
        ):
            self.optimizer.optimize_and_launch(installation, statuses.append)

        self.assertEqual(windows.apply_calls, 1)
        self.assertEqual(windows.restore_calls, 1)
        self.assertFalse(windows.mutated)
        self.assertEqual(self.power.snapshot_calls, 1)
        self.assertEqual(self.power.apply_calls, 1)
        self.assertEqual(self.power.restore_calls, 1)
        self.assertFalse(self.optimizer.is_running)
        self.assertFalse(self.store.active_path.exists())

        archives = list(self.store.directory.glob("session_*.json"))
        self.assertEqual(len(archives), 1)
        archived = StateSnapshot.from_dict(json.loads(archives[0].read_text(encoding="utf-8")))
        self.assertFalse(archived.active)
        self.assertEqual(archived.phase, "RESTORED")
        self.assertFalse(archived.registry_values[0].applied)
        self.assertIsNotNone(archived.power_mode)
        self.assertIsNone(archived.power_mode.target_ac_mode_guid)
        self.assertIn("Restore complete", statuses)

    def test_running_game_is_adopted_without_launching_a_duplicate(self) -> None:
        installation = self._installation(create_files=True)
        running = FakeDetectedGameProcess(
            4242,
            time.time() - 120.0,
            Path(installation.executable),
        )
        identity = ProcessIdentity(
            pid=running.pid,
            name=running.name(),
            create_time=running.create_time(),
            priority=running.nice(),
            affinity=running.cpu_affinity(),
            action="game",
            executable=running.exe(),
        )
        watch_result = GameWatchResult(
            WatchStopReason.GAME_EXITED,
            (identity,),
            (identity,),
            (),
        )
        windows = FakeWindowsTransaction()
        self.optimizer.windows = windows  # type: ignore[assignment]
        statuses: list[str] = []

        with (
            patch(
                "sas_booster.services.optimizer.psutil.process_iter",
                return_value=[running],
            ),
            patch.object(self.optimizer, "_launch") as launch,
            patch.object(
                self.optimizer,
                "_watch_game",
                return_value=watch_result,
            ) as watch_game,
        ):
            restored = self.optimizer.optimize_and_launch(installation, statuses.append)

        launch.assert_not_called()
        watch_game.assert_called_once()
        self.assertEqual(
            watch_game.call_args.kwargs["initial_identity"].key,
            identity.key,
        )
        self.assertFalse(any("launch requested" in message.lower() for message in statuses))
        self.assertFalse(restored.active)
        self.assertEqual(restored.phase, "RESTORED")

    def test_attach_only_never_relaunches_a_game_that_already_exited(self) -> None:
        installation = self._installation(create_files=True)
        statuses: list[str] = []

        with (
            patch.object(
                self.optimizer,
                "_find_matching_game_identity",
                return_value=None,
            ),
            patch.object(self.optimizer, "_launch") as launch,
        ):
            with self.assertRaises(GameLaunchError):
                self.optimizer.optimize_and_launch(
                    installation,
                    statuses.append,
                    attach_only=True,
                )

        launch.assert_not_called()
        self.assertIsNone(self.store.load_active())

    def test_running_game_detection_requires_the_exact_executable_path(self) -> None:
        installation = self._installation(create_files=True)
        wrong = FakeDetectedGameProcess(
            4100,
            time.time() - 30.0,
            self.root / "Other" / "RocketLeague.exe",
        )
        expected = FakeDetectedGameProcess(
            4200,
            time.time() - 20.0,
            Path(installation.executable),
        )

        with patch(
            "sas_booster.services.optimizer.psutil.process_iter",
            return_value=[wrong, expected],
        ):
            detected = self.optimizer.find_running_game(
                Path(installation.executable),
            )

        self.assertIsNotNone(detected)
        self.assertEqual(detected.pid, 4200)  # type: ignore[union-attr]
        self.assertEqual(detected.executable, installation.executable)  # type: ignore[union-attr]
        self.assertEqual(detected.command_line, [])  # type: ignore[union-attr]

        with patch(
            "sas_booster.services.optimizer.psutil.process_iter",
            return_value=[wrong],
        ):
            self.assertIsNone(
                self.optimizer.find_running_game(Path(installation.executable))
            )

    def test_no_running_game_launches_once_even_if_watcher_observes_a_restart(self) -> None:
        installation = self._installation(create_files=True)
        first = ProcessIdentity(501, "RocketLeague.exe", time.time() + 0.01)
        restarted = ProcessIdentity(502, "RocketLeague.exe", time.time() + 0.02)
        watch_result = GameWatchResult(
            WatchStopReason.GAME_EXITED,
            (first, restarted),
            (first, restarted),
            (),
        )
        self.optimizer.windows = FakeWindowsTransaction()  # type: ignore[assignment]

        with (
            patch(
                "sas_booster.services.optimizer.psutil.process_iter",
                return_value=[],
            ),
            patch.object(self.optimizer, "_launch") as launch,
            patch.object(
                self.optimizer,
                "_watch_game",
                return_value=watch_result,
            ),
        ):
            self.optimizer.optimize_and_launch(installation, lambda _message: None)

        launch.assert_called_once()
        launched_profile = launch.call_args.args[0]
        self.assertEqual(Path(launched_profile.executable), Path(installation.executable))

    def test_epic_launch_uses_protocol_uri_not_manifest_executable(self) -> None:
        direct = self._installation(create_files=True)
        installation = GameProfile(
            id="epic-game", name="Epic Game", platform="Epic Games",
            install_root=direct.install_root, launch_executable=direct.launch_executable,
            executable=direct.executable, launch_uri=self.EPIC_LAUNCH_URI,
        )

        with (
            patch(
                "sas_booster.services.optimizer.os.startfile",
                create=True,
            ) as startfile,
            patch("sas_booster.services.optimizer.subprocess.Popen") as popen,
        ):
            self.optimizer._launch(installation)

        startfile.assert_called_once_with(self.EPIC_LAUNCH_URI)
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
