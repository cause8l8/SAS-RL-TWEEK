# Release packaging

The installer elevates to place program files under `Program Files`, but the
installed shortcut does not request elevation and starts the application in
normal user mode. The application's separate optional service-elevation flow
remains runtime behavior rather than an installer manifest requirement.

## Prerequisites

1. Use 64-bit CPython on Windows and create a clean virtual environment.
2. Install the pinned tools with `python -m pip install -r requirements-build.txt`.
3. Add the release icon at `assets/sas_booster.ico`.
4. Install Inno Setup 6.7+ or 7 only when an installer is wanted. Its absence
   does not prevent the portable ZIP and checksums from being produced.

## Validate without building

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\build.ps1 -VerifyOnly
```

## Build an unsigned release

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\build.ps1 `
  -PyInstallerPython ".\.venv\Scripts\python.exe"
```

Artifacts are written to `release\<version>\`. Every release contains a
portable ZIP, `SHA256SUMS.txt`, and `release-manifest.json`. When a supported
Inno Setup compiler is installed, the same run also creates the installer.

The Program Files installer is the recommended distribution, especially when
administrator-only service options will be used, because ordinary user
processes cannot replace its executable or bundled DLLs. Treat an extracted
portable folder as user-writable and use it only from a trusted location.

## Optional real Authenticode signing

Signing is never simulated. The `-Sign` switch requires `signtool.exe` and a
valid code-signing certificate with a private key in the selected Windows
certificate store. With no thumbprint, the newest valid code-signing
certificate in that store is selected.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\build.ps1 `
  -PyInstallerPython ".\.venv\Scripts\python.exe" `
  -Sign `
  -CertificateStore CurrentUser `
  -CertificateThumbprint "REAL_CERTIFICATE_SHA1_THUMBPRINT"
```

The main executable, Inno Setup installer, and embedded uninstaller are signed
and timestamped when signing is enabled. The script verifies Authenticode
signatures before writing the final checksums.
