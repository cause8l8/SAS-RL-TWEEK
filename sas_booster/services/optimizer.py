"""Transactional Optimize & Launch coordinator and crash-safe restore manager."""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import psutil

from sas_booster.config import ConfigStore
from sas_booster.models import GameProfile, ProcessIdentity, StateSnapshot, utc_now
from sas_booster.services.background_apps import BackgroundAppManager
from sas_booster.services.game_process_watcher import (
    GameProcessWatcher,
    GameWatchResult,
    WatchStopReason,
)
from sas_booster.services.network import NetworkOptimizer
from sas_booster.services.power_manager import PowerPlanManager
from sas_booster.services.processes import ProcessMatchStatus, ProcessService
from sas_booster.services.session_mutex import SessionMutex
from sas_booster.services.state_store import StateStore
from sas_booster.services.windows_gaming import WindowsGamingOptimizer
from sas_booster.services.windows_settings import WindowsSettings
from sas_booster.utils.windows import CREATE_NO_WINDOW, is_windows, set_power_plan


StatusCallback = Callable[[str], None]


class RestoreIncomplete(RuntimeError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("Restoration is incomplete: " + "; ".join(errors))
        self.errors = errors


class GameLaunchError(RuntimeError):
    pass


class OptimizerService:
    """Own exactly one session from snapshot through automatic restoration."""

    def __init__(
        self,
        config: ConfigStore,
        store: StateStore | None = None,
        settings: WindowsSettings | None = None,
        processes: ProcessService | None = None,
        *,
        power: PowerPlanManager | None = None,
        game_config: object | None = None,
        network: NetworkOptimizer | None = None,
    ) -> None:
        self.config = config
        self.store = store or StateStore()
        self.settings = settings or WindowsSettings()
        self.processes = processes or ProcessService()
        self.power = power or PowerPlanManager()
        self.network = network or NetworkOptimizer()
        self.windows = WindowsGamingOptimizer(self.settings)
        self.background = BackgroundAppManager(self.processes)
        self.log = logging.getLogger("sas_booster")
        self._operation_lock = threading.RLock()
        self._cancel_event = threading.Event()
        self._idle_event = threading.Event()
        self._idle_event.set()
        self._watcher: GameProcessWatcher | None = None

    def has_active_session(self) -> bool:
        return self.store.load_active() is not None

    @property
    def is_running(self) -> bool:
        return not self._idle_event.is_set()

    def cancel_session(self) -> None:
        """Request a responsive stop; the session's finally block restores."""

        self._cancel_event.set()
        watcher = self._watcher
        if watcher is not None:
            watcher.cancel()
        self.log.info("Session cancellation requested")

    def wait_for_idle(self, timeout: float | None = None) -> bool:
        return self._idle_event.wait(timeout)

    def optimize_and_launch(
        self,
        installation: GameProfile | object,
        status: StatusCallback,
        *,
        attach_only: bool = False,
    ) -> StateSnapshot:
        """Apply, launch, watch, and always restore within one worker call."""

        installation = self._profile_from_legacy(installation)
        status = self._guard_status(status)
        with self._operation_lock, SessionMutex():
            if self.store.load_active() is not None:
                raise RuntimeError("An unfinished optimization session exists; restore it first")
            self._validate_installation(installation)
            self._cancel_event = threading.Event()
            options = self.config.as_dict()
            # A profile may override only ordinary booster choices; launch
            # identity remains owned by the selected profile itself.
            options.update(installation.settings)
            force_all_cores = bool(options.get("experimental_force_all_cores", False))
            initial_identity = self._find_matching_game_identity(
                Path(installation.executable),
                force_all_cores=force_all_cores,
            )
            if attach_only and initial_identity is None:
                raise GameLaunchError(
                    "The selected game exited before automatic optimization could attach"
                )
            snapshot = StateSnapshot(
                session_id=uuid.uuid4().hex,
                owner_pid=os.getpid(),
                game_path=installation.executable,
                platform=installation.platform,
            )
            self.store.save(snapshot)
            self._idle_event.clear()
            self.log.info("Optimization started | session=%s | platform=%s", snapshot.session_id, installation.platform)
            status("Recovery journal created")

            primary_error: BaseException | None = None
            try:
                snapshot.phase = "APPLYING"
                self.store.save(snapshot)
                self._apply_power(snapshot, status)
                self.windows.apply(snapshot, options, lambda: self.store.save(snapshot), status)
                self.network.apply(snapshot, options, lambda: self.store.save(snapshot), status)
                if initial_identity is not None:
                    status(f"{installation.name} is already running; attaching to its live process")

                snapshot.launch_started_at = time.time()
                self.store.save(snapshot)
                if initial_identity is None:
                    # Close the race where a game is started from a launcher or
                    # Steam while the reversible settings are being applied.
                    initial_identity = self._find_matching_game_identity(
                        Path(installation.executable),
                        force_all_cores=force_all_cores,
                    )
                if initial_identity is None:
                    launched = self._launch(installation)
                    launcher_pid = launched.pid if launched is not None else "protocol"
                    self.log.info(
                        "Game launch requested | platform=%s | launcher_pid=%s",
                        installation.platform,
                        launcher_pid,
                    )
                    status(f"{installation.name} launch requested through {installation.platform}")
                else:
                    self.log.info(
                    "Existing game process adopted | pid=%s",
                        initial_identity.pid,
                    )
                    status(f"{installation.name} is running (PID {initial_identity.pid}); duplicate launch skipped")

                snapshot.phase = "ACTIVE"
                self.store.save(snapshot)
                result = self._watch_game(
                    snapshot,
                    installation,
                    options,
                    status,
                    initial_identity=initial_identity,
                )
                snapshot.warnings.extend(result.errors)
                self.store.save(snapshot)
                if result.reason is WatchStopReason.LAUNCH_TIMEOUT:
                    raise GameLaunchError(f"{Path(installation.executable).name} was not detected; launcher failed or sign-in is required")
                if result.reason is WatchStopReason.CANCELLED:
                    status("Session cancelled; restoring now")
            except BaseException as exc:
                primary_error = exc
                self.log.error("Optimization session ended with error | type=%s", type(exc).__name__)
            finally:
                self._watcher = None
                try:
                    restored = self._restore_snapshot(snapshot, status, expected_session_id=snapshot.session_id)
                except BaseException as restore_error:
                    self._idle_event.set()
                    if primary_error is not None:
                        raise restore_error from primary_error
                    raise
                self._idle_event.set()

            if primary_error is not None:
                raise primary_error
            return restored

    # Compatibility name for callers upgrading from the prior release.
    optimize_launch = optimize_and_launch

    def restore(
        self,
        status: StatusCallback,
        *,
        expected_session_id: str | None = None,
    ) -> StateSnapshot | None:
        """Restore a crash journal or wait behind the active managed worker."""

        status = self._guard_status(status)
        with self._operation_lock, SessionMutex():
            snapshot = self.store.load_active()
            if snapshot is None:
                status("No active session to restore")
                return None
            return self._restore_snapshot(snapshot, status, expected_session_id=expected_session_id)

    def _guard_status(self, callback: StatusCallback) -> StatusCallback:
        """A UI/reporting failure must never interrupt apply or restoration."""

        def guarded(message: str) -> None:
            try:
                callback(message)
            except Exception as exc:
                self.log.warning("Status callback skipped | error=%s", type(exc).__name__)

        return guarded

    @staticmethod
    def _profile_from_legacy(value: GameProfile | object) -> GameProfile:
        """Accept the former installation value during a safe migration.

        This compatibility conversion is deliberately launch-only: it never
        carries forward no game-specific config editing.
        """
        if isinstance(value, GameProfile):
            return value
        executable = Path(getattr(value, "process_executable"))
        launcher = Path(getattr(value, "launch_executable", executable))
        uri = str(getattr(value, "launch_uri", "") or "")
        arguments = [str(item) for item in getattr(value, "launch_arguments", ())]
        return GameProfile(
            id="legacy-profile", name=executable.stem,
            platform=str(getattr(value, "platform", "Manual")), executable=str(executable),
            launch_executable=str(launcher), launch_arguments=arguments, launch_uri=uri,
            install_root=str(getattr(value, "install_root", executable.parent)),
        )

    @staticmethod
    def _validate_installation(installation: GameProfile) -> None:
        executable = Path(installation.executable)
        if not executable.is_file() or executable.suffix.casefold() != ".exe":
            raise FileNotFoundError("A valid game executable (.exe) is required")
        if not installation.launch_uri:
            launcher = Path(installation.launch_executable or installation.executable)
            if not launcher.is_file():
                raise FileNotFoundError("The game launcher executable was not found")

    def find_running_game(self, expected: Path) -> ProcessIdentity | None:
        """Return the exact running selected-game identity, if one is present."""

        return self._find_matching_game_identity(expected)

    def _find_matching_game_identity(
        self,
        expected: Path,
        *,
        force_all_cores: bool = False,
    ) -> ProcessIdentity | None:
        expected_text = os.path.normcase(os.path.abspath(str(expected)))
        action = "game_affinity" if force_all_cores else "game"
        try:
            processes = psutil.process_iter(["pid", "name", "create_time", "exe"])
        except (psutil.Error, OSError):
            return None
        for process in processes:
            try:
                if str(process.info.get("name") or "").casefold() != expected.name.casefold():
                    continue
                executable = process.info.get("exe")
                if executable and os.path.normcase(os.path.abspath(str(executable))) == expected_text:
                    identity = self.processes.identity(process, action=action)
                    if hasattr(identity, "command_line"):
                        identity.command_line = []
                    return identity
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError, TypeError, ValueError):
                continue
        return None

    def _matching_game_is_running(self, expected: Path) -> bool:
        """Compatibility predicate retained for older callers."""

        return self._find_matching_game_identity(expected) is not None

    def _apply_power(self, snapshot: StateSnapshot, status: StatusCallback) -> None:
        try:
            state = self.power.snapshot()
            snapshot.power_mode = state
            snapshot.power_plan_guid = state.original_plan_guid
            self.store.save(snapshot)  # write-ahead target before native mutation
            result = self.power.apply(state)
            snapshot.warnings.extend(result.warnings)
            self.store.save(snapshot)
            status(result.message)
            for warning in result.warnings:
                status(warning)
            self.log.info("Power action | changed=%s | modern=%s", result.changed, state.modern_api_supported)
        except Exception as exc:
            message = f"Power optimization skipped: {type(exc).__name__}"
            snapshot.warnings.append(message)
            self.store.save(snapshot)
            status(message)
            self.log.warning("Action failed | action=power | error=%s", type(exc).__name__)

    @staticmethod
    def _launch(installation: GameProfile) -> subprocess.Popen[bytes] | None:
        if installation.launch_uri:
            if not is_windows() or not hasattr(os, "startfile"):
                raise OSError("Epic Games protocol launch is available only on Windows")
            os.startfile(installation.launch_uri)  # type: ignore[attr-defined]
            return None
        return subprocess.Popen(
            [installation.launch_executable or installation.executable, *installation.launch_arguments],
            cwd=str(Path(installation.launch_executable or installation.executable).parent),
            shell=False,
            creationflags=CREATE_NO_WINDOW if is_windows() else 0,
        )

    def _watch_game(
        self,
        snapshot: StateSnapshot,
        installation: GameProfile,
        options: dict[str, object],
        status: StatusCallback,
        *,
        initial_identity: ProcessIdentity | None = None,
    ) -> GameWatchResult:
        priority = str(options.get("process_priority", "above_normal"))
        watcher = GameProcessWatcher(
            self.processes,
            Path(installation.executable),
            priority=priority,
            force_all_cores=bool(options.get("experimental_force_all_cores", False)),
            startup_restart_grace=60.0 if installation.launch_uri else None,
            cancel_event=self._cancel_event,
            logger=self.log,
        )
        self._watcher = watcher
        background_done = False

        def before_optimize(identity: ProcessIdentity) -> None:
            identity.applied = True  # prepared marker before process.nice/affinity
            snapshot.processes.append(identity)
            self.store.save(snapshot)

        def optimized(identity: ProcessIdentity) -> None:
            nonlocal background_done
            for saved in reversed(snapshot.processes):
                if saved.key == identity.key and saved.action.startswith("game"):
                    saved.applied = True
                    break
            self.store.save(snapshot)
            # Tune the time-sensitive game process before slower background
            # enumeration; short Epic bootstrap processes can otherwise exit
            # between validation and the priority write.
            if not background_done:
                background_done = True
                self.background.apply(
                    snapshot,
                    options,
                    lambda: self.store.save(snapshot),
                    status,
                )

        return watcher.watch(
            snapshot.launch_started_at,
            initial_identity=initial_identity,
            status=status,
            on_before_optimize=before_optimize,
            on_optimized=optimized,
        )

    def _restore_snapshot(
        self,
        snapshot: StateSnapshot,
        status: StatusCallback,
        *,
        expected_session_id: str | None,
    ) -> StateSnapshot:
        active = self.store.load_active()
        if active is None:
            raise RuntimeError("The active session journal disappeared before restoration")
        if active.session_id != snapshot.session_id:
            raise RuntimeError("Refusing to restore a different optimization session")
        if expected_session_id is not None and active.session_id != expected_session_id:
            raise RuntimeError("The requested restore session identifier does not match")
        snapshot = active
        snapshot.phase = "RESTORING"
        snapshot.errors = []
        self.store.save(snapshot)
        status("Restoring the exact pre-game state")
        self.log.info("Restore started | session=%s", snapshot.session_id)
        errors: list[str] = []

        self._restore_processes(snapshot, errors)
        self._restore_closed_applications(snapshot, errors)
        self.windows.restore(snapshot, lambda: self.store.save(snapshot), errors)
        self._restore_legacy_state(snapshot, errors)
        self._restore_power(snapshot, errors)

        if errors:
            snapshot.errors = errors
            snapshot.phase = "RESTORE_INCOMPLETE"
            snapshot.active = True
            self.store.save(snapshot)
            status("Restore incomplete; the recovery journal was kept for retry")
            self.log.error("Restore incomplete | session=%s | failures=%d", snapshot.session_id, len(errors))
            raise RestoreIncomplete(errors)

        snapshot.errors = []
        snapshot.active = False
        snapshot.phase = "RESTORED"
        snapshot.restored_at = utc_now()
        self.store.archive(snapshot)
        status("Restore complete")
        self.log.info("Restore complete | session=%s", snapshot.session_id)
        return snapshot

    def _restore_processes(self, snapshot: StateSnapshot, errors: list[str]) -> None:
        for identity in reversed(snapshot.processes):
            if not identity.applied:
                continue
            match = self.processes.match(identity)
            if match.status in {ProcessMatchStatus.EXITED, ProcessMatchStatus.PID_REUSED}:
                identity.applied = False
                self.store.save(snapshot)
                continue
            if match.status is ProcessMatchStatus.ACCESS_DENIED or match.process is None:
                errors.append(f"Restore process {identity.name}: access denied")
                continue
            try:
                process = match.process
                if identity.action == "suspend":
                    process.resume()
                if identity.priority is not None and identity.action in {
                    "lower_priority",
                    "game",
                    "game_affinity",
                }:
                    process.nice(identity.priority)
                if identity.action == "game_affinity" and identity.affinity and hasattr(process, "cpu_affinity"):
                    process.cpu_affinity(identity.affinity)
                identity.applied = False
                self.store.save(snapshot)
                self.log.info("Action restored | action=process | process=%s", identity.name)
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError, ValueError) as exc:
                errors.append(f"Restore process {identity.name}: {type(exc).__name__}")

    def _restore_closed_applications(self, snapshot: StateSnapshot, errors: list[str]) -> None:
        for state in reversed(snapshot.closed_applications):
            if not state.applied:
                continue
            try:
                self.processes.restart_closed_application(state)
                state.applied = False
                self.store.save(snapshot)
                self.log.info("Action restored | action=restart-closed-app | process=%s", state.name)
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                errors.append(f"Restart {state.name}: {type(exc).__name__}")

    def _restore_legacy_state(self, snapshot: StateSnapshot, errors: list[str]) -> None:
        for name in snapshot.paused_services:
            if any(item.name == name for item in snapshot.services):
                continue
            try:
                self.settings.resume_service(name)
            except Exception as exc:
                errors.append(f"Restore legacy service {name}: {type(exc).__name__}")
        if snapshot.schema_version == 2 and not snapshot.registry_values:
            try:
                self.settings.restore_game_mode(snapshot.game_mode_value, snapshot.game_mode_existed)
            except Exception as exc:
                errors.append(f"Restore legacy Game Mode: {type(exc).__name__}")

    def _restore_power(self, snapshot: StateSnapshot, errors: list[str]) -> None:
        if snapshot.power_mode is not None:
            try:
                self.power.restore(snapshot.power_mode)
                snapshot.power_mode.target_ac_mode_guid = None
                snapshot.power_mode.target_plan_guid = None
                snapshot.power_mode.applied = False
                self.store.save(snapshot)
                self.log.info("Action restored | action=power")
            except Exception as exc:
                errors.append(f"Restore power: {type(exc).__name__}")
            return
        if snapshot.power_plan_guid:
            try:
                set_power_plan(snapshot.power_plan_guid)
                snapshot.power_plan_guid = None
                self.store.save(snapshot)
            except Exception as exc:
                errors.append(f"Restore legacy power plan: {type(exc).__name__}")
