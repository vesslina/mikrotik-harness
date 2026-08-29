[CmdletBinding()]
param(
    [string]$BundleRoot = "",
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA "Programs\MikroTikHarness"),
    [switch]$AddToPath
)

$ErrorActionPreference = "Stop"
if ([string]::IsNullOrWhiteSpace($BundleRoot)) { $BundleRoot = $PSScriptRoot }
$bundle = (Resolve-Path -LiteralPath $BundleRoot).Path

function Test-CompatiblePython([string]$Candidate, [string]$ExpectedMinor) {
    if ([string]::IsNullOrWhiteSpace($Candidate) -or -not (Test-Path -LiteralPath $Candidate -PathType Leaf)) { return $null }
    try {
        $probe = (& $Candidate -c "import struct,sys; print(str(sys.version_info.major)+'.'+str(sys.version_info.minor)+'|'+str(struct.calcsize('P')*8))" 2>$null | Select-Object -Last 1)
        if ($LASTEXITCODE -eq 0 -and $probe -eq "$ExpectedMinor|64") {
            return (Resolve-Path -LiteralPath $Candidate).Path
        }
    }
    catch { return $null }
    return $null
}

function Find-CompatiblePython([string]$ExpectedMinor, [string]$PreferredPath) {
    $candidates = @($PreferredPath)
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        $located = (& $launcher.Source "-$ExpectedMinor" -c "import sys; print(sys.executable)" 2>$null | Select-Object -Last 1)
        if ($LASTEXITCODE -eq 0 -and $located) { $candidates += $located.Trim() }
    }
    $command = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($command -and $command.Source -notmatch '\\WindowsApps\\') { $candidates += $command.Source }
    foreach ($root in @("HKCU:\Software\Python\PythonCore", "HKLM:\Software\Python\PythonCore")) {
        $keyPath = Join-Path $root "$ExpectedMinor\InstallPath"
        if (Test-Path -LiteralPath $keyPath) {
            $key = Get-Item -LiteralPath $keyPath
            $executable = $key.GetValue("ExecutablePath")
            $installDirectory = $key.GetValue("")
            if ($executable) { $candidates += $executable }
            if ($installDirectory) { $candidates += (Join-Path $installDirectory "python.exe") }
        }
    }
    $digits = $ExpectedMinor.Replace(".", "")
    $candidates += (Join-Path $env:LOCALAPPDATA "Programs\Python\Python$digits\python.exe")
    $candidates += (Join-Path $env:ProgramFiles "Python$digits\python.exe")
    foreach ($candidate in $candidates | Where-Object { $_ } | Select-Object -Unique) {
        $compatible = Test-CompatiblePython $candidate $ExpectedMinor
        if ($compatible) { return $compatible }
    }
    return $null
}

$manifest = Join-Path $bundle "manifest.sha256"
if (-not (Test-Path -LiteralPath $manifest -PathType Leaf)) { throw "manifest.sha256 is missing." }

foreach ($line in Get-Content -LiteralPath $manifest) {
    if ($line -notmatch '^([0-9a-fA-F]{64})  (.+)$') { throw "Invalid manifest entry." }
    $expected = $Matches[1].ToLowerInvariant()
    $relative = $Matches[2].Replace("/", "\")
    $file = [IO.Path]::GetFullPath((Join-Path $bundle $relative))
    if (-not $file.StartsWith($bundle + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) { throw "Manifest path escapes bundle: $relative" }
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "Manifest file is missing: $relative" }
    $actual = (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $expected) { throw "Checksum mismatch: $relative" }
}

if (-not [Environment]::Is64BitOperatingSystem) { throw "MikroTik Harness requires 64-bit Windows." }
$bundleName = Split-Path $bundle -Leaf
$pythonInstaller = Get-ChildItem -LiteralPath (Join-Path $bundle "runtime") -Filter "python-*.exe" | Select-Object -First 1
$nodeArchive = Get-ChildItem -LiteralPath (Join-Path $bundle "runtime") -Filter "node-v*-win-x64.zip" | Select-Object -First 1
$wheel = Get-ChildItem -LiteralPath (Join-Path $bundle "wheelhouse") -Filter "mikrotik_harness-*.whl" | Select-Object -First 1
if (-not $pythonInstaller -or -not $nodeArchive -or -not $wheel) { throw "Bundle is missing Python, Node.js, or harness wheel." }
if ($pythonInstaller.Name -notmatch '^python-(3\.11|3\.12)') { throw "Unsupported bundled Python installer name." }
$pythonMinor = $Matches[1]

$target = [IO.Path]::GetFullPath($InstallRoot)
if ($target.Length -lt 12 -or $target -match '^[A-Za-z]:\\?$') { throw "Refusing to use an unsafe install path: $target" }
if ($target.Equals($bundle, [StringComparison]::OrdinalIgnoreCase) -or $bundle.StartsWith($target + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw "InstallRoot must not be the bundle directory or its parent."
}
New-Item -ItemType Directory -Force -Path $target | Out-Null
$pythonHome = Join-Path $target "runtime\python"
New-Item -ItemType Directory -Force -Path $pythonHome | Out-Null
$privatePython = Join-Path $pythonHome "python.exe"
$basePython = Find-CompatiblePython $pythonMinor $privatePython
if (-not $basePython) {
    $pythonArgs = @(
        "/quiet",
        "InstallAllUsers=0",
        "Include_launcher=0",
        "PrependPath=0",
        ('TargetDir="{0}"' -f $pythonHome)
    )
    $pythonProcess = Start-Process -FilePath $pythonInstaller.FullName -ArgumentList $pythonArgs -Wait -PassThru
    $basePython = Find-CompatiblePython $pythonMinor $privatePython
    if (-not $basePython) {
        throw "Bundled Python installer failed with exit code $($pythonProcess.ExitCode), and no compatible 64-bit Python $pythonMinor was found."
    }
}
$pythonVersion = (& $basePython --version 2>&1).Trim()
if ($pythonVersion -notmatch "Python $([regex]::Escape($pythonMinor))(\.|$)") {
    throw "Bundled Python version mismatch: $pythonVersion"
}
Write-Output "Using $pythonVersion at $basePython"

$nodeHome = Join-Path $target "runtime\node"
$nodeTemp = Join-Path $target "runtime\node.extract"
if (Test-Path -LiteralPath $nodeTemp) { Remove-Item -LiteralPath $nodeTemp -Recurse -Force }
Expand-Archive -LiteralPath $nodeArchive.FullName -DestinationPath $nodeTemp -Force
$nodeExe = Get-ChildItem -LiteralPath $nodeTemp -Filter "node.exe" -Recurse -File | Select-Object -First 1
if (-not $nodeExe) { throw "Node archive does not contain node.exe." }
if (Test-Path -LiteralPath $nodeHome) { Remove-Item -LiteralPath $nodeHome -Recurse -Force }
New-Item -ItemType Directory -Force -Path $nodeHome | Out-Null
Copy-Item -Path (Join-Path $nodeExe.Directory.FullName "*") -Destination $nodeHome -Recurse -Force
Remove-Item -LiteralPath $nodeTemp -Recurse -Force

$venv = Join-Path $target "venv"
$venvPython = Join-Path $venv "Scripts\python.exe"
if ((Test-Path -LiteralPath $venvPython -PathType Leaf) -and -not (Test-CompatiblePython $venvPython $pythonMinor)) {
    Remove-Item -LiteralPath $venv -Recurse -Force
}
if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    & $basePython -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "Unable to create application venv." }
}
& $venvPython -m pip install --upgrade --no-index --only-binary=:all: --find-links (Join-Path $bundle "wheelhouse") pip setuptools
if ($LASTEXITCODE -ne 0) { throw "Offline pip/setuptools bootstrap failed." }
& $venvPython -m pip install --force-reinstall --no-index --only-binary=:all: --find-links (Join-Path $bundle "wheelhouse") $wheel.FullName
if ($LASTEXITCODE -ne 0) { throw "Offline wheel installation failed." }

$app = Join-Path $target "app"
New-Item -ItemType Directory -Force -Path $app | Out-Null
Get-ChildItem -LiteralPath $app -Force | Where-Object { $_.Name -ne ".mth" } | Remove-Item -Recurse -Force
Copy-Item -Path (Join-Path $bundle "app\*") -Destination $app -Recurse -Force
if (Test-Path -LiteralPath (Join-Path $bundle "optional-private\.mth")) {
    Copy-Item -LiteralPath (Join-Path $bundle "optional-private\.mth") -Destination $app -Recurse -Force
}
Copy-Item -LiteralPath (Join-Path $bundle "THIRD_PARTY_NOTICES") -Destination $target -Force
Copy-Item -LiteralPath (Join-Path $bundle "LICENSE") -Destination $target -Force

$bin = Join-Path $target "bin"
New-Item -ItemType Directory -Force -Path $bin | Out-Null
$launcherLines = @(
    '@echo off',
    'set "MTH_PROJECT_ROOT=%~dp0..\app"',
    'set "PATH=%~dp0..\runtime\node;%PATH%"',
    'call "%~dp0..\venv\Scripts\mth.exe" %*'
)
$launcherLines | Set-Content -LiteralPath (Join-Path $bin "mth.cmd") -Encoding ASCII

$nodeVersion = (& (Join-Path $nodeHome "node.exe") --version 2>&1).Trim()
if ($nodeVersion -notmatch '^v(\d+)\.') { throw "Unable to determine bundled Node.js version." }
if ([int]$Matches[1] -lt 22) { throw "MikroMCP requires Node.js 22 or newer, got $nodeVersion." }
Write-Output $nodeVersion
& $venvPython -c "import asyncssh, cryptography, mcp, yaml, textual, mth"
if ($LASTEXITCODE -ne 0) { throw "Offline import smoke check failed." }
& $venvPython -m mth --help | Out-Null
if ($LASTEXITCODE -ne 0) { throw "mth smoke check failed." }
& $venvPython -m mth discover --help | Out-Null
if ($LASTEXITCODE -ne 0) { throw "mth discover smoke check failed." }
$previousPath = $env:PATH
$previousProjectRoot = $env:MTH_PROJECT_ROOT
try {
    $env:PATH = "$nodeHome;$env:PATH"
    $env:MTH_PROJECT_ROOT = $app
    & $venvPython -c "import asyncio; from mth.core.mcp_client import MikroMcpClient; tools=asyncio.run(MikroMcpClient(read_timeout=15).list_tools()); assert len(tools) >= 122, f'Incomplete MikroMCP catalog: {len(tools)} tools'"
    if ($LASTEXITCODE -ne 0) { throw "Bundled MikroMCP smoke check failed." }
}
finally {
    $env:PATH = $previousPath
    if ($null -eq $previousProjectRoot) { Remove-Item Env:MTH_PROJECT_ROOT -ErrorAction SilentlyContinue }
    else { $env:MTH_PROJECT_ROOT = $previousProjectRoot }
}

if ($AddToPath) {
    $current = [Environment]::GetEnvironmentVariable("Path", "User")
    $parts = @($current -split ";" | Where-Object { $_ })
    if ($parts -notcontains $bin) { [Environment]::SetEnvironmentVariable("Path", (($parts + $bin) -join ";"), "User") }
}
Write-Output "Installed $bundleName to $target"
