"""Guarded Windows helpers; no shell interpolation is used."""

from __future__ import annotations

import ctypes
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


CREATE_NO_WINDOW = 0x08000000


@dataclass(slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass(slots=True)
class PowerStatus:
    on_ac_power: bool | None
    battery_percent: int | None
    battery_saver: bool | None


def is_windows() -> bool:
    return sys.platform == "win32"


def is_admin() -> bool:
    if not is_windows():
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def run_command(args: Sequence[str], timeout: float = 15, check: bool = False) -> CommandResult:
    if not args or any(not isinstance(value, str) or not value or "\x00" in value for value in args):
        raise ValueError("Command arguments must be non-empty strings")
    try:
        result = subprocess.run(
            list(args),
            capture_output=True,
            text=True,
            encoding="oem" if is_windows() else "utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
            creationflags=CREATE_NO_WINDOW if is_windows() else 0,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"{args[0]} timed out after {timeout} seconds") from exc
    output = CommandResult(result.returncode, result.stdout.strip(), result.stderr.strip())
    if check and output.returncode:
        raise RuntimeError(output.stderr or output.stdout or f"{args[0]} failed")
    return output


def current_power_plan() -> tuple[str | None, str]:
    if not is_windows():
        return None, "Unavailable"
    result = run_command(["powercfg", "/getactivescheme"])
    match = re.search(r"([0-9a-fA-F-]{36})(?:\s+\((.+)\))?", result.stdout)
    if not match:
        return None, "Unknown"
    return match.group(1).lower(), (match.group(2) or "Unknown").strip()


def available_power_plans() -> dict[str, str]:
    if not is_windows():
        return {}
    result = run_command(["powercfg", "/list"])
    return {
        guid.lower(): name.strip()
        for guid, name in re.findall(r"([0-9a-fA-F-]{36})\s+\(([^)]+)\)", result.stdout)
    }


def set_power_plan(guid: str) -> None:
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", guid):
        raise ValueError("Invalid power plan GUID")
    run_command(["powercfg", "/setactive", guid], check=True)


def reveal_in_explorer(path: Path) -> None:
    if is_windows():
        os.startfile(str(path))  # type: ignore[attr-defined]


def system_power_status() -> PowerStatus:
    if not is_windows():
        return PowerStatus(None, None, None)

    class SYSTEM_POWER_STATUS(ctypes.Structure):
        _fields_ = [
            ("ACLineStatus", ctypes.c_ubyte),
            ("BatteryFlag", ctypes.c_ubyte),
            ("BatteryLifePercent", ctypes.c_ubyte),
            ("SystemStatusFlag", ctypes.c_ubyte),
            ("BatteryLifeTime", ctypes.c_ulong),
            ("BatteryFullLifeTime", ctypes.c_ulong),
        ]

    status = SYSTEM_POWER_STATUS()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
        return PowerStatus(None, None, None)
    on_ac = None if status.ACLineStatus == 255 else status.ACLineStatus == 1
    percent = None if status.BatteryLifePercent == 255 else int(status.BatteryLifePercent)
    saver = bool(status.SystemStatusFlag) if status.SystemStatusFlag in {0, 1} else None
    return PowerStatus(on_ac, percent, saver)


def request_elevation() -> bool:
    """Request a normal UAC elevation prompt for this exact application."""
    if not is_windows() or is_admin():
        return False
    if getattr(sys, "frozen", False):
        executable = Path(sys.executable)
        parameters = ""
    else:
        executable = Path(sys.executable)
        entry = Path(sys.argv[0]).resolve()
        parameters = f'"{entry}"'
    result = ctypes.windll.shell32.ShellExecuteW(
        None,
        "runas",
        str(executable),
        parameters,
        str(Path.cwd()),
        1,
    )
    return int(result) > 32
