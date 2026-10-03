# SAS Game Booster 1.0.0

SAS Game Booster is a conservative Windows 10/11 performance-session launcher
for any game with a verified `.exe` file. It stores a library of games, applies
temporary performance actions, watches the exact selected process path, and
restores the prior Windows state when the game exits.

## Features

- A per-game library with name, platform, executable path, launch command, and
  separate performance choices.
- Steam and Epic discovery. Epic entries use their launch metadata; Steam
  entries are listed from installed manifests and ask for a one-time executable
  link before optimization can run.
- Exact executable-path matching: a same-named process elsewhere is never
  treated as the selected game.
- Above Normal priority by default; High is explicit; Realtime is unavailable.
- Optional temporary Game Mode, capture, power, background-app, overlay, and
  approved Windows-service changes with crash-safe automatic restoration.
- A future-facing game-template boundary without editing any game's config,
  registry profile, shader cache, drivers, networking, or overclock settings.

## Safety

Each session is written before mutations to
`%LOCALAPPDATA%\SASGameBooster\state\active_session.json`. The next launch can
restore an unfinished session. System, security, audio, input, network, and
graphics processes remain protected. The program never changes a game's own
settings files in this release.

## Run from source

Python 3.11 or later is required.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r .\requirements.txt
python .\run.py
```

## Tests

```powershell
$env:PYTHONPATH = "$PWD\vendor"
python -X utf8 -m unittest discover -s .\tests -v
```

## Windows release

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\build.ps1 `
  -PyInstallerPython ".\.venv\Scripts\python.exe"
```
