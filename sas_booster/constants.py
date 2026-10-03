"""Application constants and explicit safety policy."""

from __future__ import annotations

import os
from pathlib import Path


APP_NAME = "SAS Game Booster"
APP_VERSION = "1.0.0"

LOCAL_APP_DATA = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
APP_DATA = LOCAL_APP_DATA / "SASGameBooster"
LOG_DIR = APP_DATA / "logs"
STATE_DIR = APP_DATA / "state"
BACKUP_DIR = APP_DATA / "backups"
CONFIG_PATH = APP_DATA / "config.json"
GAME_LIBRARY_PATH = APP_DATA / "games.json"

HIGH_PERFORMANCE_GUID = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"
ULTIMATE_PERFORMANCE_GUID = "e9a42b02-d5df-448d-aa00-03f14749eb61"
PROCESSOR_SUBGROUP_GUID = "54533251-82be-4824-96c1-47b60b740d00"
SYSTEM_COOLING_POLICY_GUID = "94d3a615-a899-4ac5-ae2b-e4d8f634367f"

# Names are normalized to lower case. This is deliberately broader than the
# target list: a false negative here can destabilize Windows; a false positive
# merely skips an optional optimization.
PROTECTED_PROCESS_NAMES = frozenset(
    {
        "system",
        "system idle process",
        "registry",
        "memory compression",
        "smss.exe",
        "csrss.exe",
        "wininit.exe",
        "services.exe",
        "lsass.exe",
        "lsaiso.exe",
        "winlogon.exe",
        "svchost.exe",
        "dwm.exe",
        "explorer.exe",
        "fontdrvhost.exe",
        "sihost.exe",
        "taskhostw.exe",
        "runtimebroker.exe",
        "audiodg.exe",
        "audioendpointbuilder.exe",
        "ctfmon.exe",
        "textinputhost.exe",
        "searchhost.exe",
        "startmenuexperiencehost.exe",
        "shellexperiencehost.exe",
        "msmpeng.exe",
        "nissrv.exe",
        "securityhealthservice.exe",
        "securityhealthsystray.exe",
        "smartscreen.exe",
        "senseir.exe",
        "wudfhost.exe",
        "dashost.exe",
        "spoolsv.exe",
        "conhost.exe",
        "dllhost.exe",
        "taskmgr.exe",
        # AMD graphics/display stack and control services.
        "atiesrxx.exe",
        "atieclxx.exe",
        "amdrsserv.exe",
        "amdrssrcext.exe",
        "amdfendrsr.exe",
        "amdow.exe",
        "amdppcuf.exe",
        "radeonsoftware.exe",
        "cncmd.exe",
        # Common audio, network, and input vendor services.
        "nahimicservice.exe",
        "realtekservice.exe",
        "rtkauduservice64.exe",
        "asushotplugctrl.exe",
        "asusoptimization.exe",
        "asusoptimizationstartup.exe",
        "avastsvc.exe",
        "avgsvc.exe",
    }
)

PROTECTED_SERVICE_NAMES = frozenset(
    {
        "AudioEndpointBuilder",
        "Audiosrv",
        "BFE",
        "CoreMessagingRegistrar",
        "CryptSvc",
        "Dhcp",
        "Dnscache",
        "DPS",
        "EventLog",
        "MpsSvc",
        "PlugPlay",
        "Power",
        "RpcEptMapper",
        "RpcSs",
        "SecurityHealthService",
        "WinDefend",
        "wscsvc",
        "WudfSvc",
    }
)

ALLOWED_SESSION_SERVICES = frozenset({"WSearch", "SysMain", "DoSvc"})

BACKGROUND_FAMILIES: dict[str, tuple[str, ...]] = {
    "Chrome": ("chrome.exe",),
    "Microsoft Edge": ("msedge.exe",),
    "Firefox": ("firefox.exe",),
    "OneDrive": ("onedrive.exe",),
    "Microsoft Teams": ("ms-teams.exe", "teams.exe"),
    "Adobe": ("creative cloud.exe", "ccxprocess.exe", "adobe desktop service.exe", "armsvc.exe"),
    "Epic Web Helpers": ("epicwebhelper.exe",),
    "Steam Web Helpers": ("steamwebhelper.exe",),
    "RGB Software": ("lightingservice.exe", "armourycrate.usersessionhelper.exe", "lghub.exe", "icue.exe"),
    "Overwolf": ("overwolf.exe", "overwolfbrowser.exe"),
    "Medal": ("medal.exe", "medalencoder.exe"),
    "Outplayed": ("outplayed.exe",),
}

DEFAULT_BACKGROUND_FAMILIES = (
    "Chrome",
    "Microsoft Edge",
    "Firefox",
    "OneDrive",
    "Microsoft Teams",
    "Adobe",
    "Epic Web Helpers",
    "Steam Web Helpers",
    "RGB Software",
    "Overwolf",
    "Medal",
    "Outplayed",
)
