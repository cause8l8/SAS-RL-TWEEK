"""Low-overhead exact game-process discovery and lifetime tracking.

The watcher is intentionally session-scoped.  It detects and tunes the real
selected executable, including a replacement process created during a
short restart window, but it never restores global state itself.  The session
coordinator remains responsible for journaling and restoration in ``finally``.

Callbacks run on the caller's thread.  A GUI callback must therefore marshal
work to the Tk main thread instead of touching widgets directly.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import psutil

from sas_booster.models import ProcessIdentity
from sas_booster.services.processes import ProcessMatchStatus, ProcessService


StatusCallback = Callable[[str], None]
ProcessCallback = Callable[[ProcessIdentity], None]
ProcessKey = tuple[int, float]


class GamePriority(str, Enum):
    """The only priority classes exposed by the game watcher."""

    ABOVE_NORMAL = "above_normal"
    HIGH = "high"


class WatchStopReason(str, Enum):
    GAME_EXITED = "game_exited"
    LAUNCH_TIMEOUT = "launch_timeout"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class GameWatchResult:
    """Outcome returned to the optimization-session coordinator."""

    reason: WatchStopReason
    detected: tuple[ProcessIdentity, ...]
    optimized: tuple[ProcessIdentity, ...]
    errors: tuple[str, ...]

    @property
    def game_was_detected(self) -> bool:
        return bool(self.detected)


class _IdentityState(Enum):
    RUNNING = 1
    EXITED_OR_REUSED = 2
    ACCESS_DENIED = 3


class GameProcessWatcher:
    """Detect, tune, and wait for one verified game executable.

    One instance represents one launch session.  ``launch_started_at`` passed
    to :meth:`watch` must be an epoch timestamp from ``time.time()`` captured
    immediately before invoking the platform launcher or adopting an existing
    process.  ``initial_identity`` allows that pre-existing process to bypass
    the new-process creation-time filter without bypassing identity/path checks.

    ``on_before_optimize`` is invoked after PID/name/create-time/path have been
    validated and before priority is changed.  A coordinator should use this
    hook to durably journal the original :class:`ProcessIdentity`.  If the hook
    raises, the process is not modified.
    """

    def __init__(
        self,
        processes: ProcessService,
        expected_executable: Path,
        *,
        priority: GamePriority | str = GamePriority.ABOVE_NORMAL,
        force_all_cores: bool = False,
        poll_interval: float = 1.5,
        launch_timeout: float = 90.0,
        restart_grace: float = 15.0,
        startup_restart_grace: float | None = None,
        startup_handoff_window: float = 120.0,
        creation_time_slack: float = 1.0,
        cancel_event: threading.Event | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        expected = Path(expected_executable)
        if expected.suffix.casefold() != ".exe":
            raise ValueError("expected_executable must point to an .exe file")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        if launch_timeout <= 0:
            raise ValueError("launch_timeout must be positive")
        if restart_grace < 0:
            raise ValueError("restart_grace cannot be negative")
        if startup_restart_grace is not None and startup_restart_grace < 0:
            raise ValueError("startup_restart_grace cannot be negative")
        if startup_handoff_window < 0:
            raise ValueError("startup_handoff_window cannot be negative")
        if creation_time_slack < 0:
            raise ValueError("creation_time_slack cannot be negative")

        try:
            selected_priority = GamePriority(priority)
        except ValueError as exc:
            raise ValueError("Game priority must be Above Normal or High") from exc

        self.processes = processes
        self.expected_executable = expected.resolve(strict=False)
        self.priority = selected_priority
        self.force_all_cores = bool(force_all_cores)
        # Keep the production loop deliberately slow while permitting shorter
        # injected intervals in deterministic unit tests.
        self.poll_interval = max(float(poll_interval), 0.05)
        self.launch_timeout = float(launch_timeout)
        self.restart_grace = float(restart_grace)
        self.startup_restart_grace = (
            self.restart_grace
            if startup_restart_grace is None
            else float(startup_restart_grace)
        )
        self.startup_handoff_window = float(startup_handoff_window)
        self.creation_time_slack = float(creation_time_slack)
        self.external_cancel_event = cancel_event
        self.log = logger or logging.getLogger(__name__)

        self._cancel_event = threading.Event()
        self._state_lock = threading.Lock()
        self._running = False
        self._attempted: set[ProcessKey] = set()

    @property
    def is_running(self) -> bool:
        with self._state_lock:
            return self._running

    def cancel(self) -> None:
        """Request cancellation without terminating or restoring the game."""

        self._cancel_event.set()

    def watch(
        self,
        launch_started_at: float,
        *,
        initial_identity: ProcessIdentity | None = None,
        status: StatusCallback | None = None,
        on_before_optimize: ProcessCallback | None = None,
        on_optimized: ProcessCallback | None = None,
    ) -> GameWatchResult:
        """Wait for or adopt the game, tune each incarnation once, then wait.

        A short restart grace period starts whenever the current game process
        exits.  A new matching identity is validated and optimized once.  The
        method returns on final exit, launch timeout, or cancellation and never
        invokes a restore operation.
        """

        if not isinstance(launch_started_at, (int, float)) or launch_started_at <= 0:
            raise ValueError("launch_started_at must be a positive epoch timestamp")

        with self._state_lock:
            if self._running:
                raise RuntimeError("This game-process watcher is already running")
            self._running = True
            self._attempted.clear()

        detected: list[ProcessIdentity] = []
        optimized: list[ProcessIdentity] = []
        errors: list[str] = []

        attached_to_existing = initial_identity is not None
        try:
            if initial_identity is None:
                self._notify(status, f"Waiting for {self.expected_executable.name} to start")
                identity = self._wait_for_new_identity(
                    float(launch_started_at),
                    time.monotonic() + self.launch_timeout,
                    errors,
                )
            else:
                self._notify(
                    status,
                    f"Attaching to running {self.expected_executable.name} (PID {initial_identity.pid})",
                )
                identity = initial_identity
            if identity is None:
                if self._is_cancelled():
                    self._notify(status, "Game process watch cancelled")
                    return self._result(WatchStopReason.CANCELLED, detected, optimized, errors)
                self._notify(status, f"{self.expected_executable.name} was not detected before the launch timeout")
                return self._result(WatchStopReason.LAUNCH_TIMEOUT, detected, optimized, errors)

            while identity is not None:
                detected.append(identity)
                self._notify(status, f"Detected {self.expected_executable.name} (PID {identity.pid})")
                tuned_identity = self._optimize(
                    identity,
                    errors,
                    status=status,
                    on_before_optimize=on_before_optimize,
                    on_optimized=on_optimized,
                )
                if tuned_identity is not None:
                    optimized.append(tuned_identity)

                if not self._wait_for_exit(identity):
                    self._notify(status, "Game process watch cancelled")
                    return self._result(WatchStopReason.CANCELLED, detected, optimized, errors)

                restart_grace = self._restart_grace_for(
                    launch_started_at=float(launch_started_at),
                    identity_created_at=identity.create_time,
                    first_identity=len(detected) == 1,
                    attached_to_existing=attached_to_existing,
                )
                if restart_grace == 0:
                    break
                self._notify(
                    status,
                    f"Game exited; watching {restart_grace:.0f}s for a safe restart",
                )
                identity = self._wait_for_new_identity(
                    float(launch_started_at),
                    time.monotonic() + restart_grace,
                    errors,
                )
                if self._is_cancelled():
                    self._notify(status, "Game process watch cancelled")
                    return self._result(WatchStopReason.CANCELLED, detected, optimized, errors)
                if identity is not None:
                    self._notify(status, "A restarted game process was detected")

            self._notify(status, "Game session ended")
            return self._result(WatchStopReason.GAME_EXITED, detected, optimized, errors)
        finally:
            with self._state_lock:
                self._running = False

    def _restart_grace_for(
        self,
        *,
        launch_started_at: float,
        identity_created_at: float,
        first_identity: bool,
        attached_to_existing: bool,
    ) -> float:
        """Allow a longer first-process handoff during Epic/EAC startup."""

        recent_existing_process = (
            attached_to_existing
            and launch_started_at - identity_created_at <= self.startup_handoff_window
        )
        if first_identity and (
            recent_existing_process
            or (
                not attached_to_existing
                and time.time() - launch_started_at <= self.startup_handoff_window
            )
        ):
            return max(self.restart_grace, self.startup_restart_grace)
        return self.restart_grace

    @staticmethod
    def _result(
        reason: WatchStopReason,
        detected: list[ProcessIdentity],
        optimized: list[ProcessIdentity],
        errors: list[str],
    ) -> GameWatchResult:
        return GameWatchResult(reason, tuple(detected), tuple(optimized), tuple(errors))

    def _wait_for_new_identity(
        self,
        launch_started_at: float,
        deadline: float,
        errors: list[str],
    ) -> ProcessIdentity | None:
        while not self._is_cancelled():
            candidates = self._find_candidates(launch_started_at, errors)
            if candidates:
                return min(candidates, key=lambda item: item.create_time)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            self._wait(min(self.poll_interval, remaining))
        return None

    def _find_candidates(
        self,
        launch_started_at: float,
        errors: list[str],
    ) -> list[ProcessIdentity]:
        candidates: list[ProcessIdentity] = []
        try:
            iterator = list(psutil.process_iter(["pid", "name", "create_time", "exe"]))
        except (psutil.Error, OSError) as exc:
            self._record_error(errors, f"Process enumeration failed: {exc}")
            return candidates

        for process in iterator:
            try:
                info = process.info
                name = str(info.get("name") or "")
                if name.casefold() != self.expected_executable.name.casefold():
                    continue
                create_time = float(info["create_time"])
                if create_time + self.creation_time_slack < launch_started_at:
                    continue
                executable = info.get("exe")
                if not executable or not self._same_path(Path(str(executable)), self.expected_executable):
                    continue

                # ProcessService captures the original priority and affinity,
                # and provides the canonical PID/name/create-time validation
                # used again immediately before modification.
                identity = self.processes.identity(
                    process,
                    action="game_affinity" if self.force_all_cores else "game",
                )
                self._discard_command_line(identity)
                key = self._key(identity)
                if key in self._attempted:
                    continue
                if identity.name.casefold() != self.expected_executable.name.casefold():
                    continue
                if identity.create_time + self.creation_time_slack < launch_started_at:
                    continue
                if not self._process_has_expected_path(process):
                    continue
                candidates.append(identity)
            except (KeyError, TypeError, ValueError):
                continue
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                # Access to one candidate must not stop discovery of another.
                continue
        return candidates

    def _optimize(
        self,
        identity: ProcessIdentity,
        errors: list[str],
        *,
        status: StatusCallback | None,
        on_before_optimize: ProcessCallback | None,
        on_optimized: ProcessCallback | None,
    ) -> ProcessIdentity | None:
        key = self._key(identity)
        # Mark before invoking external code so a callback failure cannot cause
        # repeated optimization attempts against the same process identity.
        self._attempted.add(key)

        match = self.processes.match(identity)
        if match.status is not ProcessMatchStatus.MATCHED or match.process is None:
            self._record_error(
                errors,
                f"PID {identity.pid} could not be safely matched before tuning ({match.status.value})",
            )
            return None
        process = match.process
        if not self._process_has_expected_path(process):
            self._record_error(errors, f"PID {identity.pid} executable path changed before tuning")
            return None
        if on_before_optimize is not None:
            try:
                on_before_optimize(identity)
            except Exception as exc:
                # Nothing has been changed yet, so a failed durable-journal
                # callback safely prevents the optimization.
                self._record_error(
                    errors,
                    f"Game pre-optimization callback failed for PID {identity.pid}: {exc}",
                )
                return None

        try:
            # ProcessService owns the priority policy and rejects non-game
            # processes.  Affinity remains untouched unless a separate,
            # explicitly enabled experimental action requests it.
            tuned_identity = self.processes.tune_game(
                process,
                self.priority.value,
                force_all_cores=self.force_all_cores,
            )
            self._discard_command_line(tuned_identity)
            self._notify(
                status,
                "Game priority set to "
                + ("Above Normal" if self.priority is GamePriority.ABOVE_NORMAL else "High"),
            )
        except Exception as exc:
            self._record_error(errors, f"Could not tune game PID {identity.pid}: {exc}")
            return None

        if on_optimized is not None:
            try:
                on_optimized(tuned_identity)
            except Exception as exc:
                # Priority has already changed, so retain the tuned identity in
                # the result even if the post-apply callback fails.  The durable
                # pre-apply record still contains the original priority.
                self._record_error(
                    errors,
                    f"Game post-optimization callback failed for PID {identity.pid}: {exc}",
                )
        return tuned_identity

    def _wait_for_exit(self, identity: ProcessIdentity) -> bool:
        """Return True after exit/PID reuse and False after cancellation."""

        while not self._is_cancelled():
            state = self._identity_state(identity)
            if state is _IdentityState.EXITED_OR_REUSED:
                return True
            # ACCESS_DENIED is deliberately treated as still running.  Treating
            # it as exit could make the coordinator restore while the game is
            # active.  Cancellation remains responsive via Event.wait().
            self._wait(self.poll_interval)
        return False

    def _identity_state(self, identity: ProcessIdentity) -> _IdentityState:
        match = self.processes.match(identity)
        if match.status in {ProcessMatchStatus.EXITED, ProcessMatchStatus.PID_REUSED}:
            return _IdentityState.EXITED_OR_REUSED
        if match.status is ProcessMatchStatus.ACCESS_DENIED or match.process is None:
            return _IdentityState.ACCESS_DENIED
        try:
            process = match.process
            if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
                return _IdentityState.EXITED_OR_REUSED
            return _IdentityState.RUNNING
        except psutil.NoSuchProcess:
            return _IdentityState.EXITED_OR_REUSED
        except (psutil.AccessDenied, OSError):
            return (
                _IdentityState.ACCESS_DENIED
                if psutil.pid_exists(identity.pid)
                else _IdentityState.EXITED_OR_REUSED
            )

    def _process_has_expected_path(self, process: psutil.Process) -> bool:
        try:
            executable = process.exe()
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            return False
        return bool(executable) and self._same_path(Path(executable), self.expected_executable)

    @staticmethod
    def _same_path(left: Path, right: Path) -> bool:
        try:
            if left.exists() and right.exists() and os.path.samefile(left, right):
                return True
        except OSError:
            pass
        try:
            left_text = os.path.normpath(os.path.abspath(os.fspath(left))).casefold()
            right_text = os.path.normpath(os.path.abspath(os.fspath(right))).casefold()
        except (OSError, TypeError, ValueError):
            return False
        return left_text == right_text

    @staticmethod
    def _key(identity: ProcessIdentity) -> ProcessKey:
        return identity.pid, round(identity.create_time, 6)

    @staticmethod
    def _discard_command_line(identity: ProcessIdentity) -> None:
        """Do not retain launcher/authentication arguments for the game."""

        if hasattr(identity, "command_line"):
            identity.command_line = []

    def _is_cancelled(self) -> bool:
        return self._cancel_event.is_set() or bool(
            self.external_cancel_event and self.external_cancel_event.is_set()
        )

    def _wait(self, timeout: float) -> None:
        if timeout <= 0:
            return
        # The private event makes cancel() responsive.  An externally supplied
        # event is checked at each deliberately low-frequency iteration.
        self._cancel_event.wait(timeout)

    def _notify(self, callback: StatusCallback | None, message: str) -> None:
        self.log.info(message)
        if callback is None:
            return
        try:
            callback(message)
        except Exception:
            self.log.exception("Status callback failed")

    def _record_error(self, errors: list[str], message: str) -> None:
        if not errors or errors[-1] != message:
            errors.append(message)
        self.log.warning(message)
