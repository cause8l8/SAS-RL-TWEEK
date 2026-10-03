[CmdletBinding()]
param(
    [Alias("Python")]
    [string]$PyInstallerPython = "python",

    [switch]$SkipTests,
    [switch]$SkipInstaller,
    [switch]$VerifyOnly,

    [switch]$Sign,
    [string]$CertificateThumbprint = "",
    [ValidateSet("CurrentUser", "LocalMachine")]
    [string]$CertificateStore = "CurrentUser",
    [string]$SignToolPath = "",
    [string]$TimestampUrl = "http://timestamp.digicert.com",

    [string]$InnoSetupPath = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $MyInvocation.MyCommand.Path))
$appName = "SAS Game Booster"
$specPath = Join-Path $projectRoot "SAS Game Booster.spec"
$installerScript = Join-Path $projectRoot "packaging\installer\SASGameBooster.iss"
$iconPath = Join-Path $projectRoot "assets\sas_booster.ico"
$buildRoot = Join-Path $projectRoot "build"
$distRoot = Join-Path $projectRoot "dist"
$appDist = Join-Path $distRoot $appName


function Resolve-Executable {
    param(
        [Parameter(Mandatory = $true)][string]$Value,
        [Parameter(Mandatory = $true)][string]$DisplayName
    )

    if ([System.IO.Path]::IsPathRooted($Value) -or $Value.Contains("\") -or $Value.Contains("/")) {
        $candidate = $Value
        if (-not [System.IO.Path]::IsPathRooted($candidate)) {
            $candidate = Join-Path $projectRoot $candidate
        }
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            throw "$DisplayName was not found: $candidate"
        }
        return (Resolve-Path -LiteralPath $candidate).Path
    }

    $command = Get-Command $Value -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $command) {
        throw "$DisplayName was not found on PATH: $Value"
    }
    return $command.Source
}


function Assert-ProjectChildPath {
    param([Parameter(Mandatory = $true)][string]$Path)

    $fullPath = [System.IO.Path]::GetFullPath($Path)
    $rootPrefix = $projectRoot.TrimEnd("\", "/") + [System.IO.Path]::DirectorySeparatorChar
    if (-not $fullPath.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to modify a path outside the project: $fullPath"
    }
    return $fullPath
}


function Reset-Directory {
    param([Parameter(Mandatory = $true)][string]$Path)

    $safePath = Assert-ProjectChildPath $Path
    if (Test-Path -LiteralPath $safePath) {
        Remove-Item -LiteralPath $safePath -Recurse -Force
    }
    New-Item -ItemType Directory -Path $safePath -Force | Out-Null
}


function Invoke-NativeCommand {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$ArgumentList,
        [Parameter(Mandatory = $true)][string]$FailureMessage
    )

    & $FilePath @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw "$FailureMessage (exit code $LASTEXITCODE)"
    }
}


function Write-Utf8NoBom {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Value
    )

    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Value, $encoding)
}


function Get-ReleaseVersion {
    $sources = [ordered]@{
        "sas_booster/constants.py" = @('APP_VERSION\s*=\s*["'']([^"'']+)["'']', 1)
        "sas_booster/__init__.py" = @('__version__\s*=\s*["'']([^"'']+)["'']', 1)
        "version_info.txt" = @("StringStruct\('ProductVersion',\s*'([^']+)'\)", 1)
        "README.md" = @("(?m)^# SAS Game Booster\s+([^\s]+)", 1)
    }

    $values = @{}
    foreach ($relativePath in $sources.Keys) {
        $path = Join-Path $projectRoot $relativePath
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "Required version source is missing: $relativePath"
        }
        $pattern = [string]$sources[$relativePath][0]
        $group = [int]$sources[$relativePath][1]
        $match = [regex]::Match((Get-Content -LiteralPath $path -Raw), $pattern)
        if (-not $match.Success) {
            throw "Could not read the version from $relativePath"
        }
        $values[$relativePath] = $match.Groups[$group].Value
    }

    $uniqueVersions = @($values.Values | Select-Object -Unique)
    if ($uniqueVersions.Count -ne 1) {
        $details = ($values.GetEnumerator() | Sort-Object Key | ForEach-Object { "$($_.Key)=$($_.Value)" }) -join "; "
        throw "Release version sources disagree: $details"
    }

    $version = [string]$uniqueVersions[0]
    if ($version -notmatch '^\d+\.\d+\.\d+$') {
        throw "Release version must use numeric MAJOR.MINOR.PATCH format: $version"
    }
    return $version
}


function Assert-ReleaseInputs {
    param([switch]$AllowMissingIcon)

    $requiredFiles = @(
        "run.py",
        "version_info.txt",
        "requirements.txt",
        "requirements-build.txt",
        "SAS Game Booster.spec",
        "packaging\installer\SASGameBooster.iss",
        "assets\sas_booster.png",
        "vendor\customtkinter\assets\fonts\CustomTkinter_shapes_font.otf",
        "vendor\customtkinter\assets\fonts\Roboto\Roboto-Medium.ttf",
        "vendor\customtkinter\assets\fonts\Roboto\Roboto-Regular.ttf",
        "vendor\customtkinter\assets\icons\CustomTkinter_icon_Windows.ico",
        "vendor\customtkinter\assets\themes\blue.json",
        "vendor\customtkinter\assets\themes\dark-blue.json",
        "vendor\customtkinter\assets\themes\green.json"
    )
    foreach ($relativePath in $requiredFiles) {
        if (-not (Test-Path -LiteralPath (Join-Path $projectRoot $relativePath) -PathType Leaf)) {
            throw "Required release input is missing: $relativePath"
        }
    }

    if (-not (Test-Path -LiteralPath $iconPath -PathType Leaf)) {
        if ($AllowMissingIcon) {
            Write-Warning "Release icon is pending: assets\sas_booster.ico"
        }
        else {
            throw "Release icon is missing: assets\sas_booster.ico"
        }
    }
    else {
        $iconBytes = [System.IO.File]::ReadAllBytes($iconPath)
        if (
            $iconBytes.Length -lt 22 -or
            $iconBytes[0] -ne 0 -or
            $iconBytes[1] -ne 0 -or
            $iconBytes[2] -ne 1 -or
            $iconBytes[3] -ne 0
        ) {
            throw "Release icon is not a valid Windows ICO file: assets\sas_booster.ico"
        }
        $iconImageCount = [BitConverter]::ToUInt16($iconBytes, 4)
        if ($iconImageCount -lt 7) {
            throw "Release ICO must contain at least seven resolution layers; found $iconImageCount"
        }
    }
}


function Resolve-SignTool {
    param([string]$ExplicitPath)

    if ($ExplicitPath) {
        return Resolve-Executable $ExplicitPath "SignTool"
    }

    $onPath = Get-Command "signtool.exe" -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $onPath) {
        return $onPath.Source
    }

    $kitsRoot = Join-Path ${env:ProgramFiles(x86)} "Windows Kits\10\bin"
    if (Test-Path -LiteralPath $kitsRoot -PathType Container) {
        $candidate = Get-ChildItem -LiteralPath $kitsRoot -Filter "signtool.exe" -File -Recurse -ErrorAction SilentlyContinue |
            Where-Object { $_.DirectoryName -match '[\\/]x64$' } |
            Sort-Object LastWriteTimeUtc -Descending |
            Select-Object -First 1
        if ($null -ne $candidate) {
            return $candidate.FullName
        }
    }
    throw "Code signing was requested, but signtool.exe was not found"
}


function Resolve-CodeSigningCertificate {
    param(
        [string]$RequestedThumbprint,
        [string]$StoreLocation
    )

    $storePath = "Cert:\$StoreLocation\My"
    $requested = ($RequestedThumbprint -replace '\s', '').ToUpperInvariant()
    if ($requested) {
        if ($requested -notmatch '^[0-9A-F]{40}$') {
            throw "CertificateThumbprint must be a 40-character SHA-1 thumbprint"
        }
        $certificate = Get-Item -LiteralPath "$storePath\$requested" -ErrorAction SilentlyContinue
        if ($null -eq $certificate) {
            throw "The requested certificate was not found in $storePath"
        }
    }
    else {
        $certificate = Get-ChildItem -LiteralPath $storePath -CodeSigningCert -ErrorAction SilentlyContinue |
            Where-Object { $_.HasPrivateKey -and $_.NotAfter -gt (Get-Date) } |
            Sort-Object NotAfter -Descending |
            Select-Object -First 1
        if ($null -eq $certificate) {
            throw "Code signing was requested, but no valid code-signing certificate with a private key exists in $storePath"
        }
    }

    $now = Get-Date
    $codeSigningOid = "1.3.6.1.5.5.7.3.3"
    $enhancedKeyUsageOids = @($certificate.EnhancedKeyUsageList | ForEach-Object { $_.ObjectId.Value })
    if (
        -not $certificate.HasPrivateKey -or
        $certificate.NotBefore -gt $now -or
        $certificate.NotAfter -le $now -or
        $codeSigningOid -notin $enhancedKeyUsageOids
    ) {
        throw "The selected certificate is not a currently valid code-signing certificate with a private key"
    }
    return $certificate
}


function Invoke-SignFile {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)]$SigningContext
    )

    $arguments = @(
        "sign",
        "/fd", "SHA256",
        "/td", "SHA256",
        "/tr", $TimestampUrl,
        "/sha1", $SigningContext.Certificate.Thumbprint,
        "/d", $appName
    )
    if ($CertificateStore -eq "LocalMachine") {
        $arguments += "/sm"
    }
    $arguments += $FilePath
    Invoke-NativeCommand $SigningContext.SignTool $arguments "Authenticode signing failed for $FilePath"
    Invoke-NativeCommand $SigningContext.SignTool @("verify", "/pa", "/v", $FilePath) "Authenticode verification failed for $FilePath"
}


function Resolve-InnoSetup {
    param([string]$ExplicitPath)

    if ($ExplicitPath) {
        return Resolve-Executable $ExplicitPath "Inno Setup compiler"
    }

    $onPath = Get-Command "ISCC.exe" -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $onPath) {
        return $onPath.Source
    }

    $candidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 7\ISCC.exe"),
        (Join-Path $env:ProgramFiles "Inno Setup 7\ISCC.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 7\ISCC.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
        (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe")
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return $candidate
        }
    }
    return $null
}


function Get-InnoSignCommand {
    param([Parameter(Mandatory = $true)]$SigningContext)

    $storeFlag = if ($CertificateStore -eq "LocalMachine") { "/sm " } else { "" }
    $fileToken = '$f'
    $quoteToken = '$q'
    return ('{0}{1}{0} sign /fd SHA256 /td SHA256 /tr {0}{2}{0} {3}/sha1 {4} /d {0}{5}{0} {6}' -f `
        $quoteToken,
        $SigningContext.SignTool,
        $TimestampUrl,
        $storeFlag,
        $SigningContext.Certificate.Thumbprint,
        $appName,
        $fileToken)
}


$version = Get-ReleaseVersion
Assert-ReleaseInputs -AllowMissingIcon:$VerifyOnly

if ($VerifyOnly) {
    Write-Host "Release configuration is internally consistent for version $version."
    Write-Host "No application, archive, installer, or signature was produced."
    exit 0
}

$python = Resolve-Executable $PyInstallerPython "Python interpreter"
$pythonInfo = & $python -I -X utf8 -c "import platform, struct; print(platform.machine()); print(struct.calcsize('P') * 8); print(platform.python_version())"
if ($LASTEXITCODE -ne 0 -or @($pythonInfo).Count -lt 3) {
    throw "Unable to inspect the release Python interpreter"
}
if ([string]$pythonInfo[0] -notmatch '^(?i:AMD64|x86_64)$' -or [string]$pythonInfo[1] -ne "64") {
    throw "The win-x64 release must be built with 64-bit x86 Python; found $($pythonInfo -join ' / ')"
}

$expectedPyInstallerMatch = [regex]::Match(
    (Get-Content -LiteralPath (Join-Path $projectRoot "requirements-build.txt") -Raw),
    "(?m)^pyinstaller==([^\s]+)$"
)
if (-not $expectedPyInstallerMatch.Success) {
    throw "requirements-build.txt must pin PyInstaller with =="
}
$expectedPyInstaller = $expectedPyInstallerMatch.Groups[1].Value
$actualPyInstaller = & $python -I -X utf8 -c "import PyInstaller; print(PyInstaller.__version__)"
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller is unavailable in the isolated release environment; install requirements-build.txt into a clean virtual environment"
}
if ([string]$actualPyInstaller -ne $expectedPyInstaller) {
    throw "PyInstaller version mismatch: expected $expectedPyInstaller, found $actualPyInstaller"
}

$signingContext = $null
if ($Sign) {
    $signingContext = [pscustomobject]@{
        SignTool = Resolve-SignTool $SignToolPath
        Certificate = Resolve-CodeSigningCertificate $CertificateThumbprint $CertificateStore
    }
    Write-Host "Authenticode signing enabled with certificate $($signingContext.Certificate.Thumbprint)."
}
else {
    Write-Host "Authenticode signing is disabled. Use -Sign only when a real code-signing certificate is available."
}

$artifactRoot = Join-Path (Join-Path $projectRoot "release") $version
$portableZip = Join-Path $artifactRoot "SAS-Game-Booster-$version-win-x64-portable.zip"
$installerName = "SAS-Game-Booster-$version-win-x64-setup.exe"
$installerPath = Join-Path $artifactRoot $installerName

$oldPythonNoUserSite = [Environment]::GetEnvironmentVariable("PYTHONNOUSERSITE", "Process")
$oldPythonPath = [Environment]::GetEnvironmentVariable("PYTHONPATH", "Process")
[Environment]::SetEnvironmentVariable("PYTHONNOUSERSITE", "1", "Process")
[Environment]::SetEnvironmentVariable("PYTHONPATH", $null, "Process")

Push-Location $projectRoot
try {
    if (-not $SkipTests) {
        Invoke-NativeCommand $python @("-I", "-X", "utf8", "-m", "unittest", "discover", "-s", ".\tests", "-v") "Release tests failed"
    }

    Reset-Directory $buildRoot
    Reset-Directory $distRoot
    Reset-Directory $artifactRoot

    Invoke-NativeCommand $python @(
        "-I",
        "-X", "utf8",
        "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--distpath", $distRoot,
        "--workpath", $buildRoot,
        $specPath
    ) "PyInstaller build failed"

    $appExe = Join-Path $appDist "$appName.exe"
    if (-not (Test-Path -LiteralPath $appExe -PathType Leaf)) {
        throw "PyInstaller did not produce the expected executable: $appExe"
    }

    Copy-Item -LiteralPath (Join-Path $projectRoot "README.md") -Destination (Join-Path $appDist "README.md") -Force
    $thirdPartyNotices = Join-Path $projectRoot "THIRD_PARTY_NOTICES.txt"
    if (Test-Path -LiteralPath $thirdPartyNotices -PathType Leaf) {
        Copy-Item -LiteralPath $thirdPartyNotices -Destination $appDist -Force
    }

    if ($null -ne $signingContext) {
        Invoke-SignFile $appExe $signingContext
    }

    Compress-Archive -LiteralPath $appDist -DestinationPath $portableZip -CompressionLevel Optimal -Force

    if (-not $SkipInstaller) {
        $innoSetup = Resolve-InnoSetup $InnoSetupPath
        if ($null -eq $innoSetup) {
            Write-Warning "Inno Setup 6.3+ or 7 was not found. Portable ZIP creation will continue without an installer."
        }
        else {
            $oldVersion = [Environment]::GetEnvironmentVariable("SAS_BOOSTER_VERSION", "Process")
            $oldSource = [Environment]::GetEnvironmentVariable("SAS_BOOSTER_SOURCE_DIR", "Process")
            $oldOutput = [Environment]::GetEnvironmentVariable("SAS_BOOSTER_OUTPUT_DIR", "Process")
            $oldProject = [Environment]::GetEnvironmentVariable("SAS_BOOSTER_PROJECT_ROOT", "Process")
            try {
                [Environment]::SetEnvironmentVariable("SAS_BOOSTER_VERSION", $version, "Process")
                [Environment]::SetEnvironmentVariable("SAS_BOOSTER_SOURCE_DIR", $appDist, "Process")
                [Environment]::SetEnvironmentVariable("SAS_BOOSTER_OUTPUT_DIR", $artifactRoot, "Process")
                [Environment]::SetEnvironmentVariable("SAS_BOOSTER_PROJECT_ROOT", $projectRoot, "Process")

                $innoArguments = @()
                if ($null -ne $signingContext) {
                    $innoSignCommand = Get-InnoSignCommand $signingContext
                    $innoArguments += "/Sbooster_sign=$innoSignCommand"
                    $innoArguments += "/DReleaseSignTool=booster_sign"
                }
                $innoArguments += $installerScript
                Invoke-NativeCommand $innoSetup $innoArguments "Inno Setup build failed"
            }
            finally {
                [Environment]::SetEnvironmentVariable("SAS_BOOSTER_VERSION", $oldVersion, "Process")
                [Environment]::SetEnvironmentVariable("SAS_BOOSTER_SOURCE_DIR", $oldSource, "Process")
                [Environment]::SetEnvironmentVariable("SAS_BOOSTER_OUTPUT_DIR", $oldOutput, "Process")
                [Environment]::SetEnvironmentVariable("SAS_BOOSTER_PROJECT_ROOT", $oldProject, "Process")
            }

            if (-not (Test-Path -LiteralPath $installerPath -PathType Leaf)) {
                throw "Inno Setup did not produce the expected installer: $installerPath"
            }
            if ($null -ne $signingContext) {
                Invoke-NativeCommand $signingContext.SignTool @("verify", "/pa", "/v", $installerPath) "Installer signature verification failed"
            }
        }
    }

    $releaseFiles = @(
        Get-ChildItem -LiteralPath $artifactRoot -File |
            Where-Object { $_.Extension -in @(".exe", ".zip") } |
            Sort-Object Name
    )
    if ($releaseFiles.Count -eq 0) {
        throw "No release artifacts were produced"
    }

    $artifacts = @()
    $checksumLines = @()
    foreach ($file in $releaseFiles) {
        $hash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToUpperInvariant()
        $checksumLines += "$hash *$($file.Name)"
        $artifacts += [ordered]@{
            name = $file.Name
            size_bytes = $file.Length
            sha256 = $hash
        }
    }

    Write-Utf8NoBom (Join-Path $artifactRoot "SHA256SUMS.txt") (($checksumLines -join "`n") + "`n")
    $manifest = [ordered]@{
        schema_version = 1
        product = $appName
        version = $version
        platform = "windows"
        architecture = "x64"
        python_version = [string]$pythonInfo[2]
        pyinstaller_version = [string]$actualPyInstaller
        authenticode_signed = ($null -ne $signingContext)
        signing_certificate_thumbprint = if ($null -ne $signingContext) { $signingContext.Certificate.Thumbprint } else { $null }
        created_utc = [DateTime]::UtcNow.ToString("o")
        artifacts = $artifacts
    }
    Write-Utf8NoBom (Join-Path $artifactRoot "release-manifest.json") (($manifest | ConvertTo-Json -Depth 5) + "`n")

    Write-Host "Release artifacts are ready in: $artifactRoot"
}
finally {
    Pop-Location
    [Environment]::SetEnvironmentVariable("PYTHONNOUSERSITE", $oldPythonNoUserSite, "Process")
    [Environment]::SetEnvironmentVariable("PYTHONPATH", $oldPythonPath, "Process")
}
