from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "vendor"))
sys.path.insert(0, str(ROOT))

import psutil

from sas_booster.models import StateSnapshot
from sas_booster.services.background_apps import BackgroundAppManager
from sas_booster.services.processes import ProcessService


_UNSET = object()


class SimulatedInterruption(BaseException):
    pass


class AlwaysSafeValidator:
    @staticmethod
    def is_safe_to_manage(_process: object) -> bool:
        return True


class FakeHelperProcess:
    def __init__(
        self,
        pid: int,
        events: list[tuple[str, int | tuple[tuple[int, bool], ...]]],
        *,
        fail_priority_write: bool = False,
        interrupt_after_priority_write: bool = False,
    ) -> None:
        self.pid = pid
        self.info = {
            "pid": pid,
            "name": "chrome.exe" if pid % 2 else "msedge.exe",
            "create_time": 1000.0 + pid,
        }
        self._events = events
        self._fail_priority_write = fail_priority_write
        self._interrupt_after_priority_write = interrupt_after_priority_write
        self.priority = 32
        self.suspended = False
        self.name_reads = 0
        self.create_time_reads = 0
        self.priority_reads = 0

    def name(self) -> str:
        self.name_reads += 1
        return str(self.info["name"])

    def create_time(self) -> float:
        self.create_time_reads += 1
        return float(self.info["create_time"])

    def nice(self, value: object = _UNSET) -> int:
        if value is _UNSET:
            self.priority_reads += 1
            return self.priority
        self._events.append(("mutation", self.pid))
        if self._fail_priority_write:
            raise psutil.AccessDenied(pid=self.pid)
        self.priority = int(value)
        if self._interrupt_after_priority_write:
            raise SimulatedInterruption
        return self.priority

    def suspend(self) -> None:
        self._events.append(("mutation", self.pid))
        self.suspended = True

    def cpu_affinity(self, _value: object = _UNSET) -> list[int]:
        raise AssertionError("background identity must not read CPU affinity")

    def exe(self) -> str:
        raise AssertionError("background identity must not read executable")

    def cmdline(self) -> list[str]:
        raise AssertionError("background identity must not read command line")


class BackgroundIdentityTests(unittest.TestCase):
    def test_lightweight_identity_reads_only_state_needed_by_each_action(self) -> None:
        events: list[tuple[str, int | tuple[tuple[int, bool], ...]]] = []
        process = FakeHelperProcess(7, events)
        service = ProcessService(validator=AlwaysSafeValidator())  # type: ignore[arg-type]

        lowered = service.background_identity(process, "lower_priority")  # type: ignore[arg-type]
        suspended = service.background_identity(process, "suspend")  # type: ignore[arg-type]

        self.assertEqual(lowered.priority, 32)
        self.assertIsNone(suspended.priority)
        self.assertEqual(lowered.affinity, [])
        self.assertEqual(lowered.executable, "")
        self.assertEqual(lowered.command_line, [])
        self.assertEqual(process.priority_reads, 1)


class BackgroundBatchTests(unittest.TestCase):
    @staticmethod
    def options(action: str = "lower_priority") -> dict[str, object]:
        return {
            "background_families": ["Chrome", "Microsoft Edge"],
            "background_action": action,
            "keep_discord": True,
        }

    def run_batch(
        self,
        *,
        failed_pid: int | None = None,
    ) -> tuple[
        StateSnapshot,
        list[FakeHelperProcess],
        list[tuple[str, int | tuple[tuple[int, bool], ...]]],
        list[str],
    ]:
        events: list[tuple[str, int | tuple[tuple[int, bool], ...]]] = []
        helpers = [
            FakeHelperProcess(pid, events, fail_priority_write=pid == failed_pid)
            for pid in range(100, 130)
        ]
        snapshot = StateSnapshot("background-batch")
        statuses: list[str] = []
        service = ProcessService(validator=AlwaysSafeValidator())  # type: ignore[arg-type]
        manager = BackgroundAppManager(service)

        def persist() -> None:
            saved = tuple((identity.pid, identity.applied) for identity in snapshot.processes)
            events.append(("persist", saved))

        with patch(
            "sas_booster.services.background_apps.psutil.process_iter",
            return_value=helpers,
        ):
            manager.apply(snapshot, self.options(), persist, statuses.append)

        return snapshot, helpers, events, statuses

    def test_thirty_helpers_use_one_write_ahead_and_one_final_persist(self) -> None:
        snapshot, helpers, events, statuses = self.run_batch()

        persists = [event for event in events if event[0] == "persist"]
        mutations = [event for event in events if event[0] == "mutation"]
        self.assertEqual(len(persists), 2)
        self.assertEqual(len(mutations), 30)
        self.assertEqual(events[0][0], "persist")
        self.assertEqual(events[-1][0], "persist")
        self.assertEqual(len(persists[0][1]), 30)  # type: ignore[arg-type]
        self.assertTrue(all(applied for _pid, applied in persists[0][1]))  # type: ignore[union-attr]
        self.assertEqual(len(snapshot.processes), 30)
        self.assertEqual(len(statuses), 2)
        self.assertTrue(any("chrome.exe" in message and "15 processes" in message for message in statuses))
        self.assertTrue(any("msedge.exe" in message and "15 processes" in message for message in statuses))
        self.assertTrue(all(process.priority_reads == 1 for process in helpers))
        self.assertTrue(all(process.name_reads == 0 for process in helpers))
        self.assertTrue(all(process.create_time_reads == 0 for process in helpers))

    def test_one_failed_helper_does_not_block_the_remaining_batch(self) -> None:
        snapshot, _helpers, events, statuses = self.run_batch(failed_pid=115)

        mutations = [pid for kind, pid in events if kind == "mutation"]
        persists = [saved for kind, saved in events if kind == "persist"]
        failed = next(identity for identity in snapshot.processes if identity.pid == 115)
        self.assertEqual(len(mutations), 30)
        self.assertIn(129, mutations)
        self.assertEqual(len(persists), 2)
        self.assertTrue(dict(persists[0])[115])  # type: ignore[arg-type]
        self.assertFalse(dict(persists[-1])[115])  # type: ignore[arg-type]
        self.assertFalse(failed.applied)
        self.assertEqual(len(statuses), 2)
        self.assertTrue(any("chrome.exe" in message and "14 processes" in message for message in statuses))
        self.assertTrue(any("msedge.exe" in message and "15 processes" in message for message in statuses))

    def test_missing_create_time_is_skipped_without_aborting_the_batch(self) -> None:
        events: list[tuple[str, int | tuple[tuple[int, bool], ...]]] = []
        incomplete = FakeHelperProcess(115, events)
        incomplete.info["create_time"] = None
        snapshot = StateSnapshot("background-missing-create-time")
        service = ProcessService(validator=AlwaysSafeValidator())  # type: ignore[arg-type]
        manager = BackgroundAppManager(service)

        with patch(
            "sas_booster.services.background_apps.psutil.process_iter",
            return_value=[incomplete],
        ):
            manager.apply(
                snapshot,
                self.options(),
                lambda: events.append(("persist", ())),
                lambda _message: None,
            )

        self.assertEqual(snapshot.processes, [])
        self.assertEqual(events, [])

    def test_failed_write_ahead_persist_prevents_every_priority_mutation(self) -> None:
        events: list[tuple[str, int | tuple[tuple[int, bool], ...]]] = []
        helpers = [FakeHelperProcess(pid, events) for pid in range(200, 230)]
        snapshot = StateSnapshot("background-interruption")
        service = ProcessService(validator=AlwaysSafeValidator())  # type: ignore[arg-type]
        manager = BackgroundAppManager(service)

        def interrupted_persist() -> None:
            saved = tuple((identity.pid, identity.applied) for identity in snapshot.processes)
            events.append(("persist", saved))
            raise OSError("simulated journal interruption")

        with patch(
            "sas_booster.services.background_apps.psutil.process_iter",
            return_value=helpers,
        ):
            with self.assertRaises(OSError):
                manager.apply(snapshot, self.options(), interrupted_persist, lambda _message: None)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], "persist")
        self.assertEqual(len(events[0][1]), 30)  # type: ignore[arg-type]
        self.assertTrue(all(applied for _pid, applied in events[0][1]))  # type: ignore[union-attr]
        self.assertTrue(all(process.priority == 32 for process in helpers))

    def test_interruption_after_first_mutation_has_the_entire_batch_in_journal(self) -> None:
        events: list[tuple[str, int | tuple[tuple[int, bool], ...]]] = []
        helpers = [
            FakeHelperProcess(
                pid,
                events,
                interrupt_after_priority_write=pid == 200,
            )
            for pid in range(200, 230)
        ]
        snapshot = StateSnapshot("background-mid-batch-interruption")
        service = ProcessService(validator=AlwaysSafeValidator())  # type: ignore[arg-type]
        manager = BackgroundAppManager(service)

        def persist() -> None:
            saved = tuple((identity.pid, identity.applied) for identity in snapshot.processes)
            events.append(("persist", saved))

        with patch(
            "sas_booster.services.background_apps.psutil.process_iter",
            return_value=helpers,
        ):
            with self.assertRaises(SimulatedInterruption):
                manager.apply(snapshot, self.options(), persist, lambda _message: None)

        self.assertEqual([kind for kind, _value in events], ["persist", "mutation"])
        journal = events[0][1]
        self.assertEqual(len(journal), 30)  # type: ignore[arg-type]
        self.assertTrue(all(applied for _pid, applied in journal))  # type: ignore[union-attr]
        self.assertNotEqual(helpers[0].priority, 32)
        self.assertTrue(all(process.priority == 32 for process in helpers[1:]))

    def test_suspend_keeps_per_process_write_ahead_journaling(self) -> None:
        events: list[tuple[str, int | tuple[tuple[int, bool], ...]]] = []
        helpers = [FakeHelperProcess(pid, events) for pid in range(300, 303)]
        snapshot = StateSnapshot("background-suspend")
        service = ProcessService(validator=AlwaysSafeValidator())  # type: ignore[arg-type]
        manager = BackgroundAppManager(service)

        def persist() -> None:
            saved = tuple((identity.pid, identity.applied) for identity in snapshot.processes)
            events.append(("persist", saved))

        with patch(
            "sas_booster.services.background_apps.psutil.process_iter",
            return_value=helpers,
        ):
            manager.apply(snapshot, self.options("suspend"), persist, lambda _message: None)

        self.assertEqual([kind for kind, _value in events], [
            "persist",
            "mutation",
            "persist",
            "mutation",
            "persist",
            "mutation",
        ])
        self.assertTrue(all(process.suspended for process in helpers))
        self.assertTrue(all(process.priority_reads == 0 for process in helpers))


if __name__ == "__main__":
    unittest.main()
