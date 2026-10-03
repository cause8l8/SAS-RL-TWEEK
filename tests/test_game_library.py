from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sas_booster.models import GameProfile
from sas_booster.services.game_library import GameLibrary, PlatformDiscovery


class GameLibraryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_profiles_round_trip_with_independent_settings(self) -> None:
        library = GameLibrary(self.root / "games.json")
        first = GameProfile("a", "Game A", executable=r"C:\Games\A.exe", settings={"process_priority": "high"})
        second = GameProfile("b", "Game B", executable=r"D:\Games\B.exe", settings={"keep_discord": True})
        library.save([first, second])
        loaded = library.load()
        self.assertEqual([game.name for game in loaded], ["Game A", "Game B"])
        self.assertEqual(loaded[0].settings["process_priority"], "high")
        self.assertTrue(loaded[1].settings["keep_discord"])

    def test_epic_discovery_requires_a_real_executable(self) -> None:
        manifests = self.root / "ProgramData" / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests"
        game = self.root / "EpicGames" / "Example" / "ExampleGame.exe"
        game.parent.mkdir(parents=True); game.touch(); manifests.mkdir(parents=True)
        (manifests / "example.item").write_text(json.dumps({"AppName": "Example", "DisplayName": "Example Game", "InstallLocation": str(game.parent), "LaunchExecutable": game.name}), encoding="utf-8")
        found = PlatformDiscovery({"PROGRAMDATA": str(self.root / "ProgramData")}).discover()
        match = next(item for item in found if item.app_id == "Example")
        self.assertEqual(match.platform, "Epic")
        self.assertEqual(Path(match.executable), game)

    def test_steam_discovery_creates_an_unlinked_profile(self) -> None:
        steam = self.root / "Steam"; apps = steam / "steamapps"; apps.mkdir(parents=True)
        (apps / "appmanifest_480.acf").write_text('"AppState"\n{\n "appid" "480"\n "name" "Spacewar"\n "installdir" "Spacewar"\n}', encoding="utf-8")
        found = PlatformDiscovery({"PROGRAMFILES(X86)": str(self.root)}).discover()
        match = next(item for item in found if item.app_id == "480")
        self.assertEqual(match.platform, "Steam")
        self.assertEqual(match.executable, "")
        self.assertEqual(match.launch_arguments, ["-applaunch", "480"])
