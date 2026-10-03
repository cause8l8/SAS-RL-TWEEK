"""Durable, validated user choices for the performance engine."""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any

from sas_booster.constants import CONFIG_PATH, DEFAULT_BACKGROUND_FAMILIES


DEFAULTS: dict[str, Any] = {
    "process_priority": "above_normal",
    "experimental_force_all_cores": False,
    "background_action": "lower_priority",
    "background_families": list(DEFAULT_BACKGROUND_FAMILIES),
    "keep_discord": True,
    "custom_background_processes": [],
    "enable_game_mode": True,
    "disable_xbox_recording": True,
    "reduce_discord_overlay": False,
    "reduce_steam_overlay": False,
    "reduce_epic_overlay": False,
    "reduce_amd_metrics_overlay": False,
    "reduce_overwolf_overlay": True,
    "reduce_medal_recording": True,
    "reduce_outplayed_recording": True,
    "disable_transparency": False,
    "disable_window_animation": False,
    "pause_search": False,
    "pause_sysmain": False,
    "pause_delivery_optimization": False,
    "disable_network_power_saving": False,
    "disable_usb_selective_suspend": False,
    "flush_dns_troubleshooting": False,
    "minimize_during_game": True,
}


class ConfigStore:
    def __init__(self, path: Path = CONFIG_PATH) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._data = dict(DEFAULTS)
        self.load()

    @staticmethod
    def _sanitize(values: dict[str, Any]) -> dict[str, Any]:
        clean: dict[str, Any] = {}
        for key, default in DEFAULTS.items():
            if key not in values:
                continue
            value = values[key]
            if isinstance(default, bool):
                if isinstance(value, bool):
                    clean[key] = value
            elif isinstance(default, str):
                if isinstance(value, str):
                    clean[key] = value.strip()
            elif isinstance(default, list) and isinstance(value, list):
                clean[key] = [str(item).strip() for item in value if str(item).strip()][:64]

        if clean.get("process_priority") not in {"above_normal", "high"}:
            clean["process_priority"] = "above_normal"
        if clean.get("background_action") not in {"lower_priority", "suspend", "close"}:
            clean["background_action"] = "lower_priority"
        if "custom_background_processes" in clean:
            clean["custom_background_processes"] = [
                item.lower()
                for item in clean["custom_background_processes"]
                if re.fullmatch(r"[A-Za-z0-9_. -]{1,80}\.exe", item)
            ]
        return clean

    def load(self) -> None:
        with self._lock:
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self._data.update(self._sanitize(loaded))
            except (OSError, ValueError, TypeError):
                return

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
            try:
                with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                    json.dump(self._data, stream, indent=2, ensure_ascii=False)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.update({key: value})

    def update(self, values: dict[str, Any]) -> None:
        with self._lock:
            self._data.update(self._sanitize(values))
            self.save()

    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data)
