"""Crash-safe, validated transaction journal for one optimization session."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from sas_booster.constants import STATE_DIR
from sas_booster.models import StateSnapshot


class CorruptStateError(RuntimeError):
    pass


class StateStore:
    def __init__(self, directory: Path = STATE_DIR) -> None:
        self.directory = directory
        self._lock = threading.RLock()

    @property
    def active_path(self) -> Path:
        return self.directory / "active_session.json"

    @staticmethod
    def _atomic_json(path: Path, data: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                json.dump(data, stream, indent=2, ensure_ascii=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def save(self, snapshot: StateSnapshot) -> None:
        with self._lock:
            self._atomic_json(self.active_path, snapshot.to_dict())

    def load_active(self) -> StateSnapshot | None:
        with self._lock:
            if not self.active_path.exists():
                return None
            try:
                raw = json.loads(self.active_path.read_text(encoding="utf-8"))
                snapshot = StateSnapshot.from_dict(raw)
            except (OSError, ValueError, TypeError, KeyError) as exc:
                raise CorruptStateError(
                    "The recovery journal is damaged; optimization is blocked to protect the system."
                ) from exc
            return snapshot if snapshot.active else None

    def archive(self, snapshot: StateSnapshot) -> Path:
        """Archive only a fully restored snapshot and remove its active journal."""
        if snapshot.active or snapshot.phase != "RESTORED" or snapshot.errors:
            raise ValueError("Cannot archive an active or incompletely restored session")
        with self._lock:
            target = self.directory / f"session_{snapshot.session_id}.json"
            self._atomic_json(target, snapshot.to_dict())
            self.active_path.unlink(missing_ok=True)
            return target
