"""Identity-safe process controls with an explicit Windows protection policy."""

from __future__ import annotations

import os
import subprocess
import ctypes
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import psutil

from sas_booster.constants import PROTECTED_PROCESS_NAMES
from sas_booster.models import ClosedApplicationState, ProcessIdentity
from sas_booster.utils.windows import CREATE_NO_WINDOW, is_windows


class ProcessMatchStatus(str, Enum):
    MATCHED = "matched"
    EXITED = "exited"
    PID_REUSED = "pid_reused"
    ACCESS_DENIED = "access_denied"


@dataclass(slots=True)
class ProcessMatch:
    status: ProcessMatchStatus
    process: psutil.Process | None = None


class SafetyValidator:
    def __init__(self, extra_protected: list[str] | None = None) -> None:
        self._extra = {item.lower().strip() for item in (extra_protected or [])}
        self._windows_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        try:
            self._current_username = (psutil.Process(os.getpid()).username() or "").casefold()
        except (psutil.Error, OSError):
            self._current_username = ""
        self._service_pid_cache: set[int] = set()
        self._service_pid_cache_time = 0.0
        self._cache_lock = threading.Lock()

    def is_name_protected(self, name: str) -> bool:
        return name.lower().strip() in PROTECTED_PROCESS_NAMES | self._extra

    def is_safe_to_manage(self, process: psutil.Process) -> bool:
        if process.pid in {0, 4, os.getpid()}:
            return False
        try:
            name = process.name().lower().strip()
            if self.is_name_protected(name):
                return False
            username = (process.username() or "").lower()
            if username.endswith(("\\system", "\\local service", "\\network service")):
                return False
            if self._current_username and username.casefold() != self._current_username:
                return False
            service_pids = self._service_pids()
            if service_pids is None or process.pid in service_pids:
                return False
            executable = process.exe()
            if executable:
                try:
                    Path(executable).resolve().relative_to(self._windows_root.resolve())
                    return False
                except ValueError:
                    pass
            return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            return False

    def _service_pids(self) -> set[int] | None:
        """Cache service-host PIDs so elevated mode never manages a service."""

        if not is_windows() or not hasattr(psutil, "win_service_iter"):
            return set()
        now = time.monotonic()
        with self._cache_lock:
            if now - self._service_pid_cache_time < 5.0:
                return set(self._service_pid_cache)
            pids: set[int] = set()
            inspected = 0
            try:
                for service in psutil.win_service_iter():
                    try:
                        service_info = service.as_dict()
                        inspected += 1
                        pid = service_info.get("pid")
                        if isinstance(pid, int) and pid > 0:
                            pids.add(pid)
                    except (psutil.Error, OSError):
                        continue
            except (psutil.Error, OSError):
                # Failure to enumerate services must bias toward safety by
                # rejecting the optional process action for this scan.
                self._service_pid_cache_time = 0.0
                return None
            if inspected == 0:
                self._service_pid_cache_time = 0.0
                return None
            self._service_pid_cache = pids
            self._service_pid_cache_time = now
            return set(pids)


class ProcessService:
    def __init__(self, validator: SafetyValidator | None = None) -> None:
        self.validator = validator or SafetyValidator()

    @staticmethod
    def is_protected(name: str, extra_whitelist: list[str] | None = None) -> bool:
        return SafetyValidator(extra_whitelist).is_name_protected(name)

    def identity(self, process: psutil.Process, action: str = "") -> ProcessIdentity:
        affinity: list[int] = []
        executable = ""
        command_line: list[str] = []
        try:
            if hasattr(process, "cpu_affinity"):
                affinity = list(process.cpu_affinity())
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            pass
        try:
            executable = process.exe()
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            pass
        try:
            command_line = list(process.cmdline())
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
            pass
        return ProcessIdentity(
            pid=process.pid,
            name=process.name(),
            create_time=process.create_time(),
            priority=int(process.nice()),
            affinity=affinity,
            action=action,
            executable=executable,
            command_line=command_line,
        )

    def background_identity(
        self,
        process: psutil.Process,
        action: str,
        *,
        name: str | None = None,
        create_time: float | None = None,
    ) -> ProcessIdentity:
        """Capture only state needed to reverse a lightweight background action."""

        if action not in {"lower_priority", "suspend"}:
            raise ValueError(f"Unsupported lightweight process action {action}")
        priority = int(process.nice()) if action == "lower_priority" else None
        return ProcessIdentity(
            pid=process.pid,
            name=name if name is not None else process.name(),
            create_time=create_time if create_time is not None else process.create_time(),
            priority=priority,
            action=action,
        )

    @staticmethod
    def match(identity: ProcessIdentity) -> ProcessMatch:
        try:
            process = psutil.Process(identity.pid)
            name = process.name()
            created = process.create_time()
        except psutil.NoSuchProcess:
            return ProcessMatch(ProcessMatchStatus.EXITED)
        except (psutil.AccessDenied, OSError):
            return ProcessMatch(ProcessMatchStatus.ACCESS_DENIED)
        if name.lower() != identity.name.lower() or abs(created - identity.create_time) > 0.01:
            return ProcessMatch(ProcessMatchStatus.PID_REUSED)
        return ProcessMatch(ProcessMatchStatus.MATCHED, process)

    @classmethod
    def matches(cls, identity: ProcessIdentity) -> psutil.Process | None:
        result = cls.match(identity)
        return result.process if result.status is ProcessMatchStatus.MATCHED else None

    @staticmethod
    def game_priority(value: str) -> int:
        if not is_windows():
            return -5 if value == "high" else -2
        if value == "high":
            return int(psutil.HIGH_PRIORITY_CLASS)
        if value == "above_normal":
            return int(psutil.ABOVE_NORMAL_PRIORITY_CLASS)
        raise ValueError("Only Above Normal or High priority is permitted")

    @staticmethod
    def background_priority() -> int:
        return int(psutil.BELOW_NORMAL_PRIORITY_CLASS) if is_windows() else 5

    def tune_game(
        self,
        process: psutil.Process,
        priority: str,
        force_all_cores: bool,
    ) -> ProcessIdentity:
        identity = self.identity(process, action="game_affinity" if force_all_cores else "game")
        identity.applied = True
        process.nice(self.game_priority(priority))
        if force_all_cores and hasattr(process, "cpu_affinity"):
            process.cpu_affinity(list(range(psutil.cpu_count(logical=True) or 1)))
        return identity

    def lower_priority(self, process: psutil.Process, action: str = "lower_priority") -> ProcessIdentity:
        if not self.validator.is_safe_to_manage(process):
            raise PermissionError(f"{process.name()} is protected")
        identity = self.background_identity(process, action=action)
        identity.applied = True
        process.nice(self.background_priority())
        return identity

    def suspend_process(self, process: psutil.Process) -> ProcessIdentity:
        if not self.validator.is_safe_to_manage(process):
            raise PermissionError(f"{process.name()} is protected")
        identity = self.background_identity(process, action="suspend")
        identity.applied = True
        process.suspend()
        return identity

    def capture_closed_application(self, process: psutil.Process) -> ClosedApplicationState:
        if not self.validator.is_safe_to_manage(process):
            raise PermissionError(f"{process.name()} is protected")
        identity = self.identity(process, action="close")
        if not identity.executable or not Path(identity.executable).is_file():
            raise OSError(f"Cannot capture a restart path for {identity.name}")
        command = identity.command_line or [identity.executable]
        state = ClosedApplicationState(
            name=identity.name,
            executable=identity.executable,
            arguments=command[1:] if command and Path(command[0]).name.lower() == Path(identity.executable).name.lower() else command,
            working_directory=str(Path(identity.executable).parent),
            original_pid=identity.pid,
            original_create_time=identity.create_time,
            applied=True,
        )
        return state

    @staticmethod
    def close_captured_process(process: psutil.Process, state: ClosedApplicationState) -> bool:
        """Ask GUI windows to close; never force-kill a user application."""
        if process.pid != state.original_pid or abs(process.create_time() - state.original_create_time) > 0.01:
            raise ProcessLookupError("Process identity changed before close")
        if not is_windows():
            process.terminate()
        else:
            windows_found = False
            user32 = ctypes.windll.user32
            callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

            def close_window(hwnd: int, _lparam: int) -> bool:
                nonlocal windows_found
                owner_pid = ctypes.c_ulong()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner_pid))
                if owner_pid.value == process.pid and user32.IsWindowVisible(hwnd):
                    windows_found = True
                    user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
                return True

            user32.EnumWindows(callback_type(close_window), 0)
            if not windows_found:
                state.applied = False
                return False
        try:
            process.wait(timeout=5)
        except psutil.TimeoutExpired:
            state.applied = False
            return False
        return True

    def close_process(self, process: psutil.Process) -> ClosedApplicationState:
        state = self.capture_closed_application(process)
        self.close_captured_process(process, state)
        return state

    @staticmethod
    def restart_closed_application(state: ClosedApplicationState) -> bool:
        if not state.applied or not Path(state.executable).is_file():
            return False
        target = str(Path(state.executable).resolve()).lower()
        for process in psutil.process_iter(["name", "exe"]):
            try:
                if str(Path(process.info.get("exe") or "").resolve()).lower() == target:
                    return False
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                continue
        subprocess.Popen(
            [state.executable, *state.arguments],
            cwd=state.working_directory or str(Path(state.executable).parent),
            shell=False,
            creationflags=CREATE_NO_WINDOW if is_windows() else 0,
        )
        return True
