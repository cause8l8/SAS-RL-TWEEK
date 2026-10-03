"""Configurable, one-pass background application manager."""

from __future__ import annotations

import logging
from collections.abc import Callable

import psutil

from sas_booster.constants import BACKGROUND_FAMILIES
from sas_booster.models import ProcessIdentity, StateSnapshot
from sas_booster.services.processes import ProcessService


PersistCallback = Callable[[], None]
StatusCallback = Callable[[str], None]


OVERLAY_HELPERS: dict[str, tuple[str, ...]] = {
    "reduce_discord_overlay": ("discord.exe",),
    "reduce_steam_overlay": ("gameoverlayui.exe",),
    "reduce_epic_overlay": ("eosoverlayrenderer-win64-shipping.exe", "eosoverlayrenderer-win32-shipping.exe"),
    "reduce_overwolf_overlay": ("overwolf.exe", "overwolfbrowser.exe"),
    "reduce_medal_recording": ("medal.exe", "medalencoder.exe"),
    "reduce_outplayed_recording": ("outplayed.exe",),
}

LAUNCHER_HELPERS = frozenset({"steamwebhelper.exe", "epicwebhelper.exe"})


class BackgroundAppManager:
    def __init__(self, processes: ProcessService) -> None:
        self.processes = processes
        self.log = logging.getLogger("sas_booster")

    @staticmethod
    def target_names(options: dict[str, object]) -> tuple[set[str], set[str]]:
        regular: set[str] = set()
        for family in options.get("background_families", []):
            regular.update(name.lower() for name in BACKGROUND_FAMILIES.get(str(family), ()))
        regular.update(str(name).lower() for name in options.get("custom_background_processes", []))
        if bool(options.get("keep_discord", True)):
            regular.discard("discord.exe")

        overlay: set[str] = set()
        for key, names in OVERLAY_HELPERS.items():
            if bool(options.get(key, False)):
                overlay.update(name.lower() for name in names)
        return regular, overlay

    def apply(
        self,
        snapshot: StateSnapshot,
        options: dict[str, object],
        persist: PersistCallback,
        status: StatusCallback,
    ) -> None:
        regular, overlays = self.target_names(options)
        targets = regular | overlays
        if not targets:
            status("No background applications selected")
            return
        requested_action = str(options.get("background_action", "lower_priority"))
        handled: set[tuple[int, float]] = set()
        prepared: list[tuple[psutil.Process, str, ProcessIdentity]] = []
        for process in psutil.process_iter(["pid", "name", "create_time"]):
            try:
                captured_name = str(process.info.get("name") or "")
                name = captured_name.lower()
                if name not in targets or not self.processes.validator.is_safe_to_manage(process):
                    continue
                key = (int(process.info["pid"]), float(process.info["create_time"]))
                if key in handled:
                    continue
                handled.add(key)
                action = "lower_priority" if name in overlays or name in LAUNCHER_HELPERS else requested_action
                if action != "lower_priority":
                    self._apply_one(snapshot, process, action, persist)
                    self._report_applied(name, action, status)
                    continue
                identity = self.processes.background_identity(
                    process,
                    action,
                    name=captured_name,
                    create_time=key[1],
                )
                identity.applied = True
                snapshot.processes.append(identity)
                prepared.append((process, name, identity))
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError, TypeError, ValueError) as exc:
                self.log.warning("Background action skipped | reason=%s", type(exc).__name__)

        if prepared:
            self._apply_prepared(prepared, persist, status)

    def _apply_prepared(
        self,
        prepared: list[tuple[psutil.Process, str, ProcessIdentity]],
        persist: PersistCallback,
        status: StatusCallback,
    ) -> None:
        # One durable write-ahead journal covers the full helper-process batch.
        # Every mutation therefore has an exact PID/create-time/original-state
        # record even if the application exits in the middle of the loop.
        persist()
        priority = self.processes.background_priority()
        applied_by_name: dict[str, int] = {}
        for process, name, identity in prepared:
            try:
                process.nice(priority)
                applied_by_name[name] = applied_by_name.get(name, 0) + 1
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError, ValueError) as exc:
                identity.applied = False
                self.log.warning("Background action skipped | reason=%s", type(exc).__name__)
        # Commit successful markers and clear failed prepared markers together.
        persist()
        for name, count in applied_by_name.items():
            status(f"Background {name}: lower priority ({count} processes)")
            self.log.info(
                "Background action applied | process=%s | action=lower_priority | count=%d",
                name,
                count,
            )

    def _report_applied(self, name: str, action: str, status: StatusCallback) -> None:
        status(f"Background {name}: {action.replace('_', ' ')}")
        self.log.info("Background action applied | process=%s | action=%s", name, action)

    def _apply_one(
        self,
        snapshot: StateSnapshot,
        process: psutil.Process,
        action: str,
        persist: PersistCallback,
    ) -> None:
        if action == "close":
            state = self.processes.capture_closed_application(process)
            snapshot.closed_applications.append(state)
            persist()
            try:
                state.applied = self.processes.close_captured_process(process, state)
            except Exception:
                state.applied = False
                persist()
                raise
            persist()
            return

        identity = self.processes.background_identity(process, action=action)
        identity.applied = True
        snapshot.processes.append(identity)
        persist()
        try:
            if action == "suspend":
                process.suspend()
            elif action == "lower_priority":
                process.nice(self.processes.background_priority())
            else:
                raise ValueError(f"Unsupported background action {action}")
        except Exception:
            identity.applied = False
            persist()
            raise
