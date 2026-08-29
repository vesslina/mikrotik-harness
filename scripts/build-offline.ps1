[CmdletBinding()]
param(
    [ValidateSet("3.11", "3.12")]
    [string]$PythonVersion = "3.12",
    [string]$Python = "",
    [Parameter(Mandatory = $true)]
    [string]$PythonInstaller,
    [Parameter(Mandatory = $true)]
    [string]$NodeArchive,
    [string]$OutputRoot = "",
    [string]$RagPack = "",
    [switch]$AllowDirty,
    [switch]$SkipChecks
)

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if ([string]::IsNullOrWhiteSpace($OutputRoot)) { $OutputRoot = Join-Path $repo "dist\offline" }

function Resolve-File([string]$PathValue) {
    $resolved = Resolve-Path -LiteralPath $PathValue -ErrorAction Stop
    if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) { throw "Not a file: $PathValue" }
    return $resolved.Path
}

function Invoke-Native([string]$File, [string[]]$NativeArgs, [string]$WorkingDirectory = $repo) {
    Push-Location $WorkingDirectory
    try {
        & $File @NativeArgs
        if ($LASTEXITCODE -ne 0) { throw "$File failed with exit code $LASTEXITCODE" }
    }
    finally { Pop-Location }
}

function Copy-Tree([string]$Source, [string]$Destination) {
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    Copy-Item -Path (Join-Path $Source "*") -Destination $Destination -Recurse -Force
}

$pythonInstallerPath = Resolve-File $PythonInstaller
$nodeArchivePath = Resolve-File $NodeArchive
if ([string]::IsNullOrWhiteSpace($Python)) {
    $launcher = Get-Command py -ErrorAction Stop
    $Python = (& $launcher.Source "-$PythonVersion" "-c" "import sys; print(sys.executable)").Trim()
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($Python)) { throw "Python $PythonVersion is not installed." }
}
$pythonPath = Resolve-File $Python
$pythonVersionText = (& $pythonPath --version 2>&1).Trim()
if ($pythonVersionText -notmatch "Python $([regex]::Escape($PythonVersion))(\.|$)") {
    throw "Expected CPython $PythonVersion, got $pythonVersionText."
}

$version = (Select-String -LiteralPath (Join-Path $repo "pyproject.toml") -Pattern '^version\s*=\s*"([^"]+)"' | Select-Object -First 1).Matches.Groups[1].Value
if ([string]::IsNullOrWhiteSpace($version)) { throw "Cannot read project version from pyproject.toml." }
if ((Split-Path $pythonInstallerPath -Leaf) -notmatch "python-$([regex]::Escape($PythonVersion))") {
    throw "Python installer filename does not match CPython $PythonVersion."
}
$nodeArchiveName = Split-Path $nodeArchivePath -Leaf
if ($nodeArchiveName -notmatch '^node-v(?<major>\d+)\..*-win-x64\.zip$' -or [int]$Matches.major -lt 22) {
    throw "Node archive must be a Node 22+ win-x64 ZIP."
}

$output = [IO.Path]::GetFullPath($OutputRoot)
New-Item -ItemType Directory -Force -Path $output | Out-Null
$bundleName = "MikroTikHarness-$version-win-x64-py$($PythonVersion.Replace('.', ''))"
$stage = Join-Path $output $bundleName
if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
New-Item -ItemType Directory -Force -Path $stage | Out-Null

if (-not $AllowDirty) {
    $rootStatus = (git -C $repo status --porcelain)
    $mcpStatus = (git -C (Join-Path $repo "external\mikromcp") status --porcelain)
    if ($rootStatus -or $mcpStatus) { throw "Working tree is dirty. Commit or use -AllowDirty." }
}

$mcp = Join-Path $repo "external\mikromcp"
if (-not $SkipChecks) {
    Invoke-Native $pythonPath @("-m", "pytest") $repo
    Invoke-Native $pythonPath @("-m", "ruff", "check", "src", "tests") $repo
    Invoke-Native $pythonPath @("-m", "mypy") $repo
    Invoke-Native $pythonPath @("-m", "pip", "check") $repo
    Invoke-Native $pythonPath @("-m", "pip_audit", "--skip-editable") $repo
    & (Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe") -NoProfile -File (Join-Path $PSScriptRoot "run-mikromcp-tests.ps1")
    if ($LASTEXITCODE -ne 0) { throw "MikroMCP release tests failed." }
    Invoke-Native "npm.cmd" @("run", "typecheck") $mcp
    Invoke-Native "npm.cmd" @("run", "lint") $mcp
    Invoke-Native "npm.cmd" @("run", "build") $mcp
}

$wheelhouse = Join-Path $stage "wheelhouse"
New-Item -ItemType Directory -Force -Path $wheelhouse | Out-Null
Invoke-Native $pythonPath @("-m", "pip", "wheel", $repo, "--no-deps", "--wheel-dir", $wheelhouse)
Invoke-Native $pythonPath @(
    "-m", "pip", "download", "--only-binary=:all:", "--dest", $wheelhouse,
    "asyncssh>=2.23,<3", "cryptography>=48,<51", "mcp>=1.13,<2", "PyYAML>=6.0,<7", "textual>=8.2.8,<9",
    "pip>=26.1.2", "setuptools>=78.1.1"
)

$runtime = Join-Path $stage "runtime"
New-Item -ItemType Directory -Force -Path $runtime | Out-Null
Copy-Item -LiteralPath $pythonInstallerPath -Destination (Join-Path $runtime (Split-Path $pythonInstallerPath -Leaf))
Copy-Item -LiteralPath $nodeArchivePath -Destination (Join-Path $runtime (Split-Path $nodeArchivePath -Leaf))
Copy-Item -LiteralPath (Join-Path $repo "LICENSE") -Destination $stage
Copy-Item -LiteralPath (Join-Path $repo "THIRD_PARTY_NOTICES") -Destination $stage
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "install.ps1") -Destination $stage
Copy-Item -LiteralPath (Join-Path $PSScriptRoot "uninstall.ps1") -Destination $stage

$appMcp = Join-Path $stage "app\external\mikromcp"
$appMcpDist = Join-Path $appMcp "dist"
New-Item -ItemType Directory -Force -Path $appMcpDist | Out-Null
Copy-Item -LiteralPath (Join-Path $mcp "dist\main.js") -Destination $appMcpDist
Copy-Item -LiteralPath (Join-Path $mcp "package.json") -Destination $appMcp
Copy-Item -LiteralPath (Join-Path $mcp "package-lock.json") -Destination $appMcp
Copy-Item -LiteralPath (Join-Path $mcp "LICENSE") -Destination $appMcp
$appMcpRelative = $appMcp.Substring($repo.Length + 1).Replace("\", "/")
Invoke-Native "git" @(
    "-C", $repo, "apply", "--no-index", "--directory=$appMcpRelative",
    (Join-Path $repo "patches\mikromcp-v1.10-production-audit.patch")
) $repo
Invoke-Native "npm.cmd" @("ci", "--omit=dev") $appMcp
Copy-Tree (Join-Path $repo "docs\field-recipes") (Join-Path $stage "app\docs\field-recipes")

if ($RagPack) {
    $ragPath = (Resolve-Path -LiteralPath $RagPack -ErrorAction Stop).Path
    if (-not (Test-Path -LiteralPath (Join-Path $ragPath "manifest.json") -PathType Leaf)) {
        throw "RAG pack must contain manifest.json: $RagPack"
    }
    $optional = Join-Path $stage "optional-private\.mth"
    Copy-Tree $ragPath (Join-Path $optional "rag")
    $sidecar = "$ragPath.sha256"
    if (Test-Path -LiteralPath $sidecar -PathType Leaf) { Copy-Item -LiteralPath $sidecar -Destination (Join-Path $optional "rag.sha256") }
}

$metadata = [ordered]@{
    project = "mikrotik-harness"
    version = $version
    python = $pythonVersionText
    nodeArchive = Split-Path $nodeArchivePath -Leaf
    mikromcpCommit = (git -C $mcp rev-parse HEAD).Trim()
    builtUtc = [DateTime]::UtcNow.ToString("o")
}
$metadata | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $stage "release.json") -Encoding UTF8

$manifest = Join-Path $stage "manifest.sha256"
$lines = foreach ($file in Get-ChildItem -LiteralPath $stage -Recurse -File | Where-Object { $_.FullName -ne $manifest }) {
    $relative = $file.FullName.Substring($stage.Length + 1).Replace("\", "/")
    "$((Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant())  $relative"
}
$lines | Sort-Object | Set-Content -LiteralPath $manifest -Encoding ASCII

$zip = Join-Path $output "$bundleName.zip"
if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip -Force }
Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $zip -CompressionLevel Optimal
Write-Output "Built $stage"
Write-Output "Portable ZIP: $zip"
