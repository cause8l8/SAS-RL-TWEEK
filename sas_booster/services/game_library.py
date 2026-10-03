"""Game library persistence plus conservative Steam and Epic discovery."""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path

from sas_booster.models import GameProfile


class GameLibrary:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> list[GameProfile]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return [GameProfile.from_dict(item) for item in data.get("games", []) if isinstance(item, dict)]
        except (OSError, ValueError, TypeError):
            return []

    def save(self, games: list[GameProfile]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"games": [game.to_dict() for game in games]}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temporary, self.path)

    @staticmethod
    def manual(name: str, executable: str | Path, *, settings: dict[str, object] | None = None) -> GameProfile:
        target = Path(executable)
        return GameProfile(
            id=uuid.uuid4().hex,
            name=name.strip() or target.stem,
            executable=str(target),
            launch_executable=str(target),
            install_root=str(target.parent),
            settings=dict(settings or {}),
        )


class PlatformDiscovery:
    """Discover launch entries; Steam entries need manual executable binding."""

    def __init__(self, environ: dict[str, str] | None = None) -> None:
        self.environ = os.environ if environ is None else environ

    def discover(self) -> list[GameProfile]:
        entries = [*self._epic(), *self._steam()]
        unique: dict[tuple[str, str], GameProfile] = {}
        for entry in entries:
            unique[(entry.platform, entry.app_id or entry.name.casefold())] = entry
        return list(unique.values())

    def _epic(self) -> list[GameProfile]:
        program_data = Path(self.environ.get("PROGRAMDATA", r"C:\ProgramData"))
        manifests = program_data / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests"
        found: list[GameProfile] = []
        try:
            paths = manifests.glob("*.item")
        except OSError:
            return found
        for path in paths:
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
                root = Path(str(item.get("InstallLocation", "")))
                relative = str(item.get("LaunchExecutable", ""))
                executable = root / relative if root and relative else Path()
                if not executable.is_file():
                    continue
                app_name = str(item.get("AppName", ""))
                found.append(GameProfile(
                    id=f"epic-{app_name.casefold()}", name=str(item.get("DisplayName") or app_name or executable.stem),
                    platform="Epic", executable=str(executable), launch_uri=f"com.epicgames.launcher://apps/{app_name}?action=launch&silent=true",
                    install_root=str(root), app_id=app_name,
                ))
            except (OSError, ValueError, TypeError):
                continue
        return found

    def _steam(self) -> list[GameProfile]:
        roots = [Path(self.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Steam"]
        found: list[GameProfile] = []
        for root in roots:
            library_file = root / "steamapps" / "libraryfolders.vdf"
            libraries = [root]
            try:
                text = library_file.read_text(encoding="utf-8", errors="ignore")
                libraries.extend(Path(value) for value in re.findall(r'"path"\s+"([^"]+)"', text))
            except OSError:
                pass
            for library in libraries:
                try:
                    manifests = (library / "steamapps").glob("appmanifest_*.acf")
                except OSError:
                    continue
                for manifest in manifests:
                    try:
                        text = manifest.read_text(encoding="utf-8", errors="ignore")
                        app_id = re.search(r'"appid"\s+"(\d+)"', text)
                        name = re.search(r'"name"\s+"([^"]+)"', text)
                        folder = re.search(r'"installdir"\s+"([^"]+)"', text)
                        if not app_id or not name:
                            continue
                        install_root = library / "steamapps" / "common" / (folder.group(1) if folder else "")
                        found.append(GameProfile(
                            id=f"steam-{app_id.group(1)}", name=name.group(1), platform="Steam", app_id=app_id.group(1),
                            launch_executable=str(root / "steam.exe"), launch_arguments=["-applaunch", app_id.group(1)], install_root=str(install_root),
                        ))
                    except OSError:
                        continue
        return found
