# -*- mode: python ; coding: utf-8 -*-
"""Deterministic PyInstaller recipe for the Windows release bundle."""

from pathlib import Path


PROJECT_ROOT = Path(SPECPATH).resolve()
VENDOR_ROOT = PROJECT_ROOT / "vendor"
CTK_ASSET_ROOT = VENDOR_ROOT / "customtkinter" / "assets"
APP_ICON = PROJECT_ROOT / "assets" / "sas_booster.ico"
VERSION_INFO = PROJECT_ROOT / "version_info.txt"

# List only the CustomTkinter runtime assets the application actually needs.
# This avoids copying editor metadata or optional image plug-ins from the build
# machine into the release.
CTK_ASSETS = (
    "fonts/CustomTkinter_shapes_font.otf",
    "fonts/Roboto/Roboto-Medium.ttf",
    "fonts/Roboto/Roboto-Regular.ttf",
    "icons/CustomTkinter_icon_Windows.ico",
    "themes/blue.json",
    "themes/dark-blue.json",
    "themes/green.json",
)

datas = [
    (
        str(CTK_ASSET_ROOT / relative_path),
        str(Path("customtkinter/assets") / Path(relative_path).parent),
    )
    for relative_path in CTK_ASSETS
]
datas.append((str(APP_ICON), "assets"))

a = Analysis(
    [str(PROJECT_ROOT / "run.py")],
    pathex=[str(VENDOR_ROOT), str(PROJECT_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=["collections.abc"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "PIL",
        "charset_normalizer",
        "matplotlib",
        "numpy",
        "pandas",
        "pytest",
        "scipy",
        "win32",
        "win32com",
        "wmi",
        "_wmi",
        "yaml",
    ],
    noarchive=False,
    optimize=1,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SAS Game Booster",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    contents_directory="_internal",
    icon=str(APP_ICON) if APP_ICON.is_file() else None,
    version=str(VERSION_INFO),
    uac_admin=False,
    uac_uiaccess=False,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="SAS Game Booster",
)
