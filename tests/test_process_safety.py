from __future__ import annotations

import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "vendor"))
sys.path.insert(0, str(ROOT))

import psutil

from sas_booster.models import ProcessIdentity
from sas_booster.services.game_process_watcher import (
    GamePriority,
    GameProcessWatcher,
    WatchStopReason,
)
from sas_booster.services.processes import (
    ProcessMatch,
    ProcessMatchStatus,
    ProcessService,
    SafetyValidator,
)


_UNSET = object()


class FakeSafetyProcess:
    def __init__(self, pid: int, name: str, username: str, executable: str) -> None:
        self.pid = pid
        self._name = name
        self._username = username
        self._executable = executable

    def name(self) -> str:
        return self._name

    def username(self) -> str:
        return self._username

    def exe(self) -> str:
        return self._executable


class FakeTuneProcess:
    def __init__(self, pid: int = 777, created: float = 1000.0) -> None:
        self.pid = pid
        self._created = created
        self.priority = 32
        self.priority_written: int | None = None
        self.affinity_written: list[int] | None = None

    def name(self) -> str:
        return "RocketLeague.exe"

    def create_time(self) -> float:
        return self._created

    def nice(self, value: object = _UNSET) -> int:
        if value is _UNSET:
            return self.priority
        self.priority_written = int(value)
        return self.priority_written

    def cpu_affinity(self, value: object = _UNSET) -> list[int]:
        if value is _UNSET:
            return [0, 1, 2, 3]
        self.affinity_written = list(value)  # type: ignore[arg-type]
        return self.affinity_written

    def exe(self) -> str:
        return r"C:\Games\RocketLeague\Binaries\Win64\RocketLeague.exe"

    def cmdline(self) -> list[str]:
        return [self.exe()]


class FakeWatchedProcess:
    def __init__(self, pid: int, created: float, executable: str) -> None:
        self.pid = pid
        self._created = created
        self._executable = executable
        self.info = {
            "pid": pid,
            "name": "RocketLeague.exe",
            "create_time": created,
            "exe": executable,
        }

    def name(self) -> str:
        return "RocketLeague.exe"

    def create_time(self) -> float:
        return self._created

    def exe(self) -> str:
        return self._executable

    def is_running(self) -> bool:
        return True

    def status(self) -> str:
        return psutil.STATUS_RUNNING


class FakeWatcherProcessService:
    """Deterministic service: first match tunes, second observes exit."""

    def __init__(self, processes: dict[int, FakeWatchedProcess]) -> None:
        self.processes = processes
        self.match_calls: dict[int, int] = {}
        self.tune_calls: list[tuple[int, str, bool]] = []

    def identity(self, process: FakeWatchedProcess, action: str = "") -> ProcessIdentity:
        return ProcessIdentity(
            pid=process.pid,
            name=process.name(),
            create_time=process.create_time(),
            priority=32,
            affinity=[0, 1, 2, 3],
            action=action,
            executable=process.exe(),
        )

    def match(self, identity: ProcessIdentity) -> ProcessMatch:
        count = self.match_calls.get(identity.pid, 0)
        self.match_calls[identity.pid] = count + 1
        if count == 0:
            return ProcessMatch(ProcessMatchStatus.MATCHED, self.processes[identity.pid])  # type: ignore[arg-type]
        return ProcessMatch(ProcessMatchStatus.EXITED)

    def tune_game(
        self,
        process: FakeWatchedProcess,
        priority: str,
        force_all_cores: bool,
    ) -> ProcessIdentity:
        self.tune_calls.append((process.pid, priority, force_all_cores))
        identity = self.identity(process, action="game")
        identity.applied = True
        return identity


class FakeAdoptedProcessService(FakeWatcherProcessService):
    """Keep an already-running process matchable until it has been tuned."""

    def match(self, identity: ProcessIdentity) -> ProcessMatch:
        if any(pid == identity.pid for pid, _priority, _all_cores in self.tune_calls):
            return ProcessMatch(ProcessMatchStatus.EXITED)
        return ProcessMatch(ProcessMatchStatus.MATCHED, self.processes[identity.pid])  # type: ignore[arg-type]


class FakeExpiredInitialProcessService(FakeWatcherProcessService):
    def __init__(
        self,
        processes: dict[int, FakeWatchedProcess],
        expired_pid: int,
    ) -> None:
        super().__init__(processes)
        self.expired_pid = expired_pid

    def match(self, identity: ProcessIdentity) -> ProcessMatch:
        if identity.pid == self.expired_pid:
            return ProcessMatch(ProcessMatchStatus.EXITED)
        return super().match(identity)


class ProcessProtectionTests(unittest.TestCase):
    def test_explicit_core_driver_names_are_protected_but_games_are_manageable(self) -> None:
        validator = SafetyValidator()
        validator._current_username = r"desktop\player"

        for name in (
            "System",
            "explorer.exe",
            "audiodg.exe",
            "MsMpEng.exe",
            "AMDRSServ.exe",
            "RadeonSoftware.exe",
        ):
            with self.subTest(name=name):
                self.assertTrue(validator.is_name_protected(name))

        self.assertFalse(validator.is_name_protected("chrome.exe"))
        self.assertFalse(validator.is_name_protected("RocketLeague.exe"))
        self.assertTrue(ProcessService.is_protected("custom-helper.exe", ["CUSTOM-HELPER.EXE"]))

    def test_safety_validator_rejects_system_current_and_windows_processes(self) -> None:
        validator = SafetyValidator()
        validator._current_username = r"desktop\player"

        current = FakeSafetyProcess(
            os.getpid(),
            "python.exe",
            r"DESKTOP\Player",
            r"C:\Tools\python.exe",
        )
        system_account = FakeSafetyProcess(
            9001,
            "vendor-helper.exe",
            r"NT AUTHORITY\SYSTEM",
            r"C:\Program Files\Vendor\helper.exe",
        )
        windows_binary = FakeSafetyProcess(
            9002,
            "unlisted-helper.exe",
            r"DESKTOP\Player",
            r"C:\Windows\System32\unlisted-helper.exe",
        )
        user_application = FakeSafetyProcess(
            9003,
            "chrome.exe",
            r"DESKTOP\Player",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        )

        self.assertFalse(validator.is_safe_to_manage(current))  # type: ignore[arg-type]
        self.assertFalse(validator.is_safe_to_manage(system_account))  # type: ignore[arg-type]
        self.assertFalse(validator.is_safe_to_manage(windows_binary))  # type: ignore[arg-type]
        self.assertTrue(validator.is_safe_to_manage(user_application))  # type: ignore[arg-type]


class ProcessIdentityTests(unittest.TestCase):
    @patch("sas_booster.services.processes.psutil.Process")
    def test_pid_reuse_is_distinct_from_exit(self, process_factory) -> None:
        replacement = process_factory.return_value
        replacement.name.return_value = "RocketLeague.exe"
        replacement.create_time.return_value = 2000.0
        identity = ProcessIdentity(44, "RocketLeague.exe", 1000.0)

        match = ProcessService.match(identity)

        self.assertIs(match.status, ProcessMatchStatus.PID_REUSED)
        self.assertIsNone(match.process)

    @patch("sas_booster.services.processes.psutil.Process")
    def test_access_denied_is_not_reported_as_process_exit(self, process_factory) -> None:
        protected = process_factory.return_value
        protected.name.side_effect = psutil.AccessDenied(pid=55)
        identity = ProcessIdentity(55, "RocketLeague.exe", 1000.0)

        match = ProcessService.match(identity)

        self.assertIs(match.status, ProcessMatchStatus.ACCESS_DENIED)
        self.assertIsNone(match.process)


class ProcessPriorityTests(unittest.TestCase):
    @patch("sas_booster.services.processes.is_windows", return_value=True)
    def test_priority_policy_allows_only_above_normal_and_high(self, _windows) -> None:
        self.assertEqual(
            ProcessService.game_priority("above_normal"),
            int(psutil.ABOVE_NORMAL_PRIORITY_CLASS),
        )
        self.assertEqual(
            ProcessService.game_priority("high"),
            int(psutil.HIGH_PRIORITY_CLASS),
        )
        with self.assertRaises(ValueError):
            ProcessService.game_priority("realtime")
        with self.assertRaises(ValueError):
            GamePriority("realtime")

        default_watcher = GameProcessWatcher(
            ProcessService(),
            Path(r"C:\Games\RocketLeague.exe"),
        )
        high_watcher = GameProcessWatcher(
            ProcessService(),
            Path(r"C:\Games\RocketLeague.exe"),
            priority=GamePriority.HIGH,
        )
        self.assertIs(default_watcher.priority, GamePriority.ABOVE_NORMAL)
        self.assertIs(high_watcher.priority, GamePriority.HIGH)

    @patch("sas_booster.services.processes.is_windows", return_value=True)
    def test_default_game_tuning_does_not_write_cpu_affinity(self, _windows) -> None:
        process = FakeTuneProcess()

        identity = ProcessService().tune_game(
            process,  # type: ignore[arg-type]
            "above_normal",
            force_all_cores=False,
        )

        self.assertEqual(process.priority_written, int(psutil.ABOVE_NORMAL_PRIORITY_CLASS))
        self.assertIsNone(process.affinity_written)
        self.assertEqual(identity.affinity, [0, 1, 2, 3])
        self.assertTrue(identity.applied)


class GameProcessWatcherTests(unittest.TestCase):
    def test_adopts_preexisting_identity_without_waiting_for_a_new_process(self) -> None:
        watch_started_at = time.time()
        executable = str(
            Path(r"C:\Games\RocketLeague\Binaries\Win64\RocketLeague.exe").resolve()
        )
        running = FakeWatchedProcess(77, watch_started_at - 300.0, executable)
        service = FakeAdoptedProcessService({77: running})
        initial_identity = service.identity(running, action="game")
        watcher = GameProcessWatcher(
            service,  # type: ignore[arg-type]
            Path(executable),
            poll_interval=0.05,
            launch_timeout=0.1,
            restart_grace=0,
        )

        with patch(
            "sas_booster.services.game_process_watcher.psutil.process_iter"
        ) as process_iter:
            result = watcher.watch(
                watch_started_at,
                initial_identity=initial_identity,
            )

        self.assertIs(result.reason, WatchStopReason.GAME_EXITED)
        self.assertEqual([identity.pid for identity in result.detected], [77])
        self.assertEqual([identity.pid for identity in result.optimized], [77])
        self.assertEqual([call[0] for call in service.tune_calls], [77])
        process_iter.assert_not_called()

    def test_detects_game_that_starts_after_multiple_empty_scans_once(self) -> None:
        launch_started_at = time.time()
        executable = str(
            Path(r"C:\Games\RocketLeague\Binaries\Win64\RocketLeague.exe").resolve()
        )
        delayed = FakeWatchedProcess(303, launch_started_at + 0.01, executable)
        service = FakeWatcherProcessService({303: delayed})
        scans = iter([[], [], [delayed]])
        watcher = GameProcessWatcher(
            service,  # type: ignore[arg-type]
            Path(executable),
            poll_interval=0.05,
            launch_timeout=0.3,
            restart_grace=0,
        )

        with patch(
            "sas_booster.services.game_process_watcher.psutil.process_iter",
            side_effect=lambda *_args, **_kwargs: next(scans, []),
        ) as process_iter:
            result = watcher.watch(launch_started_at)

        self.assertIs(result.reason, WatchStopReason.GAME_EXITED)
        self.assertEqual([identity.pid for identity in result.detected], [303])
        self.assertEqual([call[0] for call in service.tune_calls], [303])
        self.assertEqual(process_iter.call_count, 3)

    def test_expired_initial_identity_falls_through_to_replacement_detection(self) -> None:
        watch_started_at = time.time()
        executable = str(
            Path(r"C:\Games\RocketLeague\Binaries\Win64\RocketLeague.exe").resolve()
        )
        expired = FakeWatchedProcess(401, watch_started_at - 60.0, executable)
        replacement = FakeWatchedProcess(402, watch_started_at + 0.01, executable)
        service = FakeExpiredInitialProcessService(
            {401: expired, 402: replacement},
            expired_pid=401,
        )
        watcher = GameProcessWatcher(
            service,  # type: ignore[arg-type]
            Path(executable),
            poll_interval=0.05,
            launch_timeout=0.2,
            restart_grace=0.15,
        )

        with patch(
            "sas_booster.services.game_process_watcher.psutil.process_iter",
            return_value=[replacement],
        ):
            result = watcher.watch(
                watch_started_at,
                initial_identity=service.identity(expired, action="game"),
            )

        self.assertIs(result.reason, WatchStopReason.GAME_EXITED)
        self.assertEqual([identity.pid for identity in result.optimized], [402])
        self.assertEqual([call[0] for call in service.tune_calls], [402])

    def test_deduplicates_identity_and_handles_one_restart(self) -> None:
        launch_started_at = time.time()
        executable = str(
            Path(r"C:\Games\RocketLeague\Binaries\Win64\RocketLeague.exe").resolve()
        )
        first = FakeWatchedProcess(101, launch_started_at + 0.01, executable)
        restarted = FakeWatchedProcess(202, launch_started_at + 0.02, executable)
        service = FakeWatcherProcessService({101: first, 202: restarted})
        # The duplicate first identity appears during restart discovery and must
        # not be tuned a second time.
        scans = iter([[first], [first], [restarted], [], [], [], []])
        statuses: list[str] = []
        before: list[tuple[int, float]] = []
        after: list[tuple[int, float]] = []
        watcher = GameProcessWatcher(
            service,  # type: ignore[arg-type]
            Path(executable),
            poll_interval=0.05,
            launch_timeout=0.4,
            restart_grace=0.13,
        )

        with patch(
            "sas_booster.services.game_process_watcher.psutil.process_iter",
            side_effect=lambda *_args, **_kwargs: next(scans, []),
        ):
            result = watcher.watch(
                launch_started_at,
                status=statuses.append,
                on_before_optimize=lambda identity: before.append(identity.key),
                on_optimized=lambda identity: after.append(identity.key),
            )

        self.assertIs(result.reason, WatchStopReason.GAME_EXITED)
        self.assertEqual([identity.pid for identity in result.detected], [101, 202])
        self.assertEqual([identity.pid for identity in result.optimized], [101, 202])
        self.assertEqual([call[0] for call in service.tune_calls], [101, 202])
        self.assertTrue(all(call[1] == "above_normal" for call in service.tune_calls))
        self.assertTrue(all(call[2] is False for call in service.tune_calls))
        self.assertEqual(before, after)
        self.assertTrue(any("restarted" in message.lower() for message in statuses))

    def test_pre_cancel_returns_without_enumerating_or_tuning(self) -> None:
        cancel = threading.Event()
        cancel.set()
        service = FakeWatcherProcessService({})
        watcher = GameProcessWatcher(
            service,  # type: ignore[arg-type]
            Path(r"C:\Games\RocketLeague.exe"),
            cancel_event=cancel,
            poll_interval=0.05,
            launch_timeout=0.1,
        )

        with patch(
            "sas_booster.services.game_process_watcher.psutil.process_iter"
        ) as process_iter:
            result = watcher.watch(time.time())

        self.assertIs(result.reason, WatchStopReason.CANCELLED)
        self.assertFalse(result.game_was_detected)
        self.assertEqual(service.tune_calls, [])
        process_iter.assert_not_called()


if __name__ == "__main__":
    unittest.main()
