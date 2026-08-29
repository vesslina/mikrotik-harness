[CmdletBinding()]
param(
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA "Programs\MikroTikHarness"),
    [switch]$PurgeState
)

$ErrorActionPreference = "Stop"
$target = [IO.Path]::GetFullPath($InstallRoot)
if (-not (Test-Path -LiteralPath $target)) { Write-Output "Nothing to uninstall."; exit 0 }
if ($target.Length -lt 12 -or $target -match '^[A-Za-z]:\\?$') { throw "Refusing to remove an unsafe install path: $target" }

$bin = Join-Path $target "bin"
$current = [Environment]::GetEnvironmentVariable("Path", "User")
if ($current) {
    $parts = @($current -split ";" | Where-Object { $_ -and $_ -ne $bin })
    [Environment]::SetEnvironmentVariable("Path", ($parts -join ";"), "User")
}

foreach ($name in @("bin", "venv", "runtime", "THIRD_PARTY_NOTICES", "LICENSE", "manifest.sha256", "release.json")) {
    $path = Join-Path $target $name
    if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Recurse -Force }
}
$app = Join-Path $target "app"
if (Test-Path -LiteralPath $app) {
    if ($PurgeState) {
        $state = Join-Path $app ".mth"
        if (Test-Path -LiteralPath $state) { Remove-Item -LiteralPath $state -Recurse -Force }
    }
    Get-ChildItem -LiteralPath $app -Force | Where-Object { $_.Name -ne ".mth" } | Remove-Item -Recurse -Force
    if ($PurgeState) { Remove-Item -LiteralPath $app -Recurse -Force }
}
if ((Get-ChildItem -LiteralPath $target -Force | Measure-Object).Count -eq 0) { Remove-Item -LiteralPath $target -Force }
Write-Output "Uninstalled application files. Private .mth state was retained: $(-not $PurgeState)"
