"""SAS Game Booster desktop interface."""

from __future__ import annotations

import queue
import sys
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from tkinter import TclError, filedialog, messagebox

import customtkinter as ctk

from sas_booster.config import ConfigStore
from sas_booster.constants import APP_NAME, APP_VERSION, BACKGROUND_FAMILIES, GAME_LIBRARY_PATH, LOG_DIR
from sas_booster.logging_utils import configure_logging
from sas_booster.models import GameProfile
from sas_booster.services.game_library import GameLibrary, PlatformDiscovery
from sas_booster.services.optimizer import GameLaunchError, OptimizerService, RestoreIncomplete
from sas_booster.services.state_store import CorruptStateError, StateStore
from sas_booster.theme import MIDNIGHT
from sas_booster.utils.windows import is_admin, reveal_in_explorer

ctk.set_appearance_mode("dark")


def _resource_path(relative: str) -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1])) / relative


class BoosterApp(ctk.CTk):
    """Library-first UI. The session engine owns all restoration work."""

    def __init__(self) -> None:
        super().__init__(fg_color=MIDNIGHT["bg"])
        icon = _resource_path("assets/sas_booster.ico")
        if icon.is_file():
            try: self.iconbitmap(str(icon))
            except (OSError, TclError): pass
        self.title(f"{APP_NAME} {APP_VERSION}")
        self.geometry("1120x760"); self.minsize(940, 650)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.log = configure_logging(); self.config_store = ConfigStore(); self.library = GameLibrary(GAME_LIBRARY_PATH)
        self.games = self.library.load(); self.state_store = StateStore(); self.optimizer = OptimizerService(self.config_store, self.state_store)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sas-session")
        self.discovery_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sas-discovery")
        self.events: queue.SimpleQueue[tuple[str, object]] = queue.SimpleQueue(); self.session: Future[object] | None = None; self.discovery: Future[object] | None = None
        self.selected_id = ctk.StringVar(value=self.games[0].id if self.games else "")
        self.profile_name = ctk.StringVar(); self.profile_path = ctk.StringVar(); self.profile_platform = ctk.StringVar(value="Manual")
        self.priority = ctk.StringVar(value="above_normal"); self.force_cores = ctk.BooleanVar(value=False); self.background_action = ctk.StringVar(value="lower_priority")
        self.keep_discord = ctk.BooleanVar(value=True); self.game_mode = ctk.BooleanVar(value=True); self.xbox_capture = ctk.BooleanVar(value=True); self.minimize = ctk.BooleanVar(value=True)
        self.family_vars: dict[str, ctk.BooleanVar] = {}
        self._build(); self._load_selected(); self.after(100, self._drain_events)

    def _build(self) -> None:
        self.grid_columnconfigure(1, weight=1); self.grid_rowconfigure(0, weight=1)
        side = ctk.CTkFrame(self, width=235, corner_radius=0, fg_color=MIDNIGHT["surface"]); side.grid(row=0, column=0, sticky="nsew"); side.grid_propagate(False)
        ctk.CTkLabel(side, text="SAS", text_color=MIDNIGHT["purple"], font=ctk.CTkFont(size=32, weight="bold")).pack(anchor="w", padx=22, pady=(28, 0))
        ctk.CTkLabel(side, text="GAME\nBOOSTER", font=ctk.CTkFont(size=16, weight="bold"), justify="left").pack(anchor="w", padx=22, pady=(0, 34))
        self.game_menu = ctk.CTkOptionMenu(side, values=self._game_menu_values(), command=self._choose_game); self.game_menu.pack(fill="x", padx=16, pady=(0, 10))
        ctk.CTkButton(side, text="DISCOVER STEAM + EPIC", command=self._discover, fg_color=MIDNIGHT["blue_button"]).pack(fill="x", padx=16, pady=5)
        ctk.CTkButton(side, text="ADD GAME", command=self._add_game).pack(fill="x", padx=16, pady=5)
        self.run_button = ctk.CTkButton(side, text="OPTIMIZE & LAUNCH", command=self._launch, fg_color=MIDNIGHT["purple"], height=46); self.run_button.pack(fill="x", padx=16, pady=(25, 5))
        ctk.CTkButton(side, text="RESTORE PREVIOUS SESSION", command=self._restore).pack(fill="x", padx=16, pady=5)
        ctk.CTkButton(side, text="OPEN LOGS", command=lambda: reveal_in_explorer(LOG_DIR)).pack(fill="x", padx=16, pady=5)
        ctk.CTkLabel(side, text="Administrator Mode" if is_admin() else "Standard Mode", text_color=MIDNIGHT["green"] if is_admin() else MIDNIGHT["yellow"]).pack(side="bottom", anchor="w", padx=18, pady=20)
        page = ctk.CTkScrollableFrame(self, fg_color=MIDNIGHT["bg"]); page.grid(row=0, column=1, sticky="nsew"); page.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(page, text="Game library", font=ctk.CTkFont(size=28, weight="bold")).grid(row=0, column=0, sticky="w", padx=28, pady=(24, 4))
        self.status = ctk.CTkLabel(page, text="Choose a game or add its executable.", text_color=MIDNIGHT["muted"]); self.status.grid(row=1, column=0, sticky="w", padx=28, pady=(0, 18))
        profile = self._panel(page, 2); self._form(profile, "Game name", self.profile_name); self._form(profile, "Executable (.exe)", self.profile_path, browse=True); self._form(profile, "Platform", self.profile_platform, choices=["Manual", "Steam", "Epic"])
        actions = ctk.CTkFrame(profile, fg_color="transparent"); actions.pack(fill="x", padx=18, pady=(10, 18)); ctk.CTkButton(actions, text="SAVE GAME", command=self._save_game, fg_color=MIDNIGHT["blue_button"]).pack(side="left"); ctk.CTkButton(actions, text="REMOVE GAME", command=self._remove_game, fg_color=MIDNIGHT["pressed"]).pack(side="left", padx=10)
        ctk.CTkLabel(page, text="Per-game performance profile", font=ctk.CTkFont(size=22, weight="bold")).grid(row=3, column=0, sticky="w", padx=28, pady=(10, 8))
        performance = self._panel(page, 4); self._form(performance, "Game priority", self.priority, choices=["above_normal", "high"]); self._switch(performance, "Use all CPU cores (experimental)", self.force_cores); self._form(performance, "Background action", self.background_action, choices=["lower_priority", "suspend", "close"])
        self._switch(performance, "Keep Discord untouched", self.keep_discord); self._switch(performance, "Enable Windows Game Mode during this session", self.game_mode); self._switch(performance, "Temporarily disable Xbox capture", self.xbox_capture); self._switch(performance, "Minimize SAS Game Booster while playing", self.minimize)
        ctk.CTkLabel(performance, text="Background applications", font=ctk.CTkFont(weight="bold")).pack(anchor="w", padx=18, pady=(12, 4)); grid = ctk.CTkFrame(performance, fg_color="transparent"); grid.pack(fill="x", padx=18, pady=(0, 15))
        for index, family in enumerate(BACKGROUND_FAMILIES):
            variable = ctk.BooleanVar(value=family in self.config_store.get("background_families", [])); self.family_vars[family] = variable
            ctk.CTkCheckBox(grid, text=family, variable=variable).grid(row=index // 3, column=index % 3, sticky="w", padx=(0, 22), pady=5)

    @staticmethod
    def _panel(parent: ctk.CTkScrollableFrame, row: int) -> ctk.CTkFrame:
        panel = ctk.CTkFrame(parent, fg_color=MIDNIGHT["surface"], corner_radius=12); panel.grid(row=row, column=0, sticky="ew", padx=28, pady=(0, 18)); return panel

    def _form(self, parent: ctk.CTkFrame, label: str, variable: ctk.StringVar, *, browse: bool = False, choices: list[str] | None = None) -> None:
        row = ctk.CTkFrame(parent, fg_color="transparent"); row.pack(fill="x", padx=18, pady=(14, 0)); ctk.CTkLabel(row, text=label, width=160, anchor="w").pack(side="left")
        (ctk.CTkOptionMenu(row, values=choices, variable=variable) if choices else ctk.CTkEntry(row, textvariable=variable)).pack(side="left", fill="x", expand=True)
        if browse: ctk.CTkButton(row, text="BROWSE", width=90, command=self._browse).pack(side="left", padx=(8, 0))

    @staticmethod
    def _switch(parent: ctk.CTkFrame, label: str, variable: ctk.BooleanVar) -> None: ctk.CTkSwitch(parent, text=label, variable=variable).pack(anchor="w", padx=18, pady=(12, 0))
    def _game_menu_values(self) -> list[str]: return [f"{game.name} · {game.platform}" for game in self.games] or ["No games added"]
    def _selected(self) -> GameProfile | None: return next((game for game in self.games if game.id == self.selected_id.get()), None)
    def _choose_game(self, value: str) -> None:
        for game in self.games:
            if value == f"{game.name} · {game.platform}": self.selected_id.set(game.id); break
        self._load_selected()
    def _load_selected(self) -> None:
        game = self._selected()
        if game is None: self.profile_name.set(""); self.profile_path.set(""); self.profile_platform.set("Manual"); return
        self.profile_name.set(game.name); self.profile_path.set(game.executable); self.profile_platform.set(game.platform); values = self.config_store.as_dict() | game.settings
        self.priority.set(str(values.get("process_priority", "above_normal"))); self.force_cores.set(bool(values.get("experimental_force_all_cores", False))); self.background_action.set(str(values.get("background_action", "lower_priority"))); self.keep_discord.set(bool(values.get("keep_discord", True))); self.game_mode.set(bool(values.get("enable_game_mode", True))); self.xbox_capture.set(bool(values.get("disable_xbox_recording", True))); self.minimize.set(bool(values.get("minimize_during_game", True)))
        for name, variable in self.family_vars.items(): variable.set(name in values.get("background_families", []))
    def _profile_settings(self) -> dict[str, object]: return {"process_priority": self.priority.get(), "experimental_force_all_cores": self.force_cores.get(), "background_action": self.background_action.get(), "keep_discord": self.keep_discord.get(), "enable_game_mode": self.game_mode.get(), "disable_xbox_recording": self.xbox_capture.get(), "minimize_during_game": self.minimize.get(), "background_families": [name for name, variable in self.family_vars.items() if variable.get()]}
    def _browse(self) -> None:
        path = filedialog.askopenfilename(title="Select game executable", filetypes=[("Windows executable", "*.exe")])
        if path: self.profile_path.set(path); self.profile_name.set(self.profile_name.get() or Path(path).stem)
    def _add_game(self) -> None:
        game = GameProfile(id=uuid.uuid4().hex, name="New game"); self.games.append(game); self.selected_id.set(game.id); self._persist_games(); self._load_selected()
    def _save_game(self) -> None:
        game = self._selected()
        if game is None: self._add_game(); game = self._selected()
        assert game is not None
        path = Path(self.profile_path.get().strip())
        if not path.is_file() or path.suffix.casefold() != ".exe": messagebox.showwarning(APP_NAME, "Choose a valid game .exe file first."); return
        game.name = self.profile_name.get().strip() or path.stem; game.executable = str(path); game.platform = self.profile_platform.get(); game.install_root = str(path.parent)
        if game.platform == "Manual": game.launch_executable = str(path); game.launch_uri = ""; game.launch_arguments = []
        game.settings = self._profile_settings(); self._persist_games(); self.status.configure(text=f"Saved {game.name}.")
    def _remove_game(self) -> None:
        game = self._selected()
        if game is None or not messagebox.askyesno(APP_NAME, f"Remove {game.name} from this library?"): return
        self.games.remove(game); self.selected_id.set(self.games[0].id if self.games else ""); self._persist_games(); self._load_selected()
    def _persist_games(self) -> None:
        self.library.save(self.games); values = self._game_menu_values(); self.game_menu.configure(values=values); self.game_menu.set(next((v for v in values if self._selected() and v.startswith(self._selected().name + " ·")), values[0]))
    def _discover(self) -> None:
        if self.discovery and not self.discovery.done(): return
        self.status.configure(text="Discovering Steam and Epic games…"); self.discovery = self.discovery_executor.submit(PlatformDiscovery().discover); self.after(100, self._finish_discovery)
    def _finish_discovery(self) -> None:
        assert self.discovery is not None
        if not self.discovery.done(): self.after(100, self._finish_discovery); return
        try: discovered = self.discovery.result()
        except Exception as exc: self.status.configure(text=f"Discovery failed: {type(exc).__name__}"); return
        existing = {(game.platform, game.app_id or game.executable.casefold()) for game in self.games}; added = 0
        for game in discovered:
            key = (game.platform, game.app_id or game.executable.casefold())
            if key not in existing: self.games.append(game); existing.add(key); added += 1
        self._persist_games(); self.status.configure(text=f"Discovery complete: {added} game(s) added. Steam games without an .exe need one-time linking.")
    def _launch(self) -> None:
        self._save_game(); game = self._selected()
        if game is None or not game.executable or (self.session and not self.session.done()): return
        try:
            if self.state_store.load_active() is not None: messagebox.showwarning(APP_NAME, "Restore the previous session first."); return
        except CorruptStateError as exc: messagebox.showerror(APP_NAME, str(exc)); return
        self.run_button.configure(state="disabled", text="SESSION RUNNING"); self.status.configure(text=f"Preparing {game.name}…"); self.session = self.executor.submit(self.optimizer.optimize_and_launch, game, self._status); self.session.add_done_callback(lambda future: self.events.put(("done", future)))
    def _restore(self) -> None: self.executor.submit(self.optimizer.restore, self._status).add_done_callback(lambda future: self.events.put(("restore", future)))
    def _status(self, message: str) -> None: self.events.put(("status", message))
    def _drain_events(self) -> None:
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "status": self.status.configure(text=str(value))
                else:
                    self.run_button.configure(state="normal", text="OPTIMIZE & LAUNCH")
                    try: value.result(); self.status.configure(text="Session restored safely.")
                    except (GameLaunchError, RestoreIncomplete, RuntimeError) as exc: messagebox.showwarning(APP_NAME, str(exc))
                    except Exception as exc: messagebox.showerror(APP_NAME, f"Operation failed: {type(exc).__name__}")
        except queue.Empty: pass
        if self.winfo_exists(): self.after(150, self._drain_events)
    def _on_close(self) -> None:
        if self.session and not self.session.done():
            if not messagebox.askyesno(APP_NAME, "Cancel the active session and restore settings before closing?"): return
            self.optimizer.cancel_session(); self.after(200, self._on_close); return
        self.executor.shutdown(wait=False, cancel_futures=True); self.discovery_executor.shutdown(wait=False, cancel_futures=True); self.destroy()


def main() -> None: BoosterApp().mainloop()
