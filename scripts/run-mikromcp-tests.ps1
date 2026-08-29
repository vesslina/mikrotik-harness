[CmdletBinding()]
param(
    [switch]$SkipNpmInstall
)

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\external\mikromcp")).Path
$patches = @(
    (Resolve-Path (Join-Path $PSScriptRoot "..\patches\mikromcp-v1.10-windows-paths.patch")).Path,
    (Resolve-Path (Join-Path $PSScriptRoot "..\patches\mikromcp-v1.10-production-audit.patch")).Path
)

foreach ($relative in @("test/unit/config/app-config.test.ts", "package-lock.json")) {
    if ((git -C $repo status --porcelain -- $relative)) {
        throw "MikroMCP $relative is already modified; refusing to overlay a release patch."
    }
}

$applied = @()
try {
    foreach ($patch in $patches) {
        git -C $repo apply --check -- $patch
        if ($LASTEXITCODE -ne 0) { throw "MikroMCP release patch does not apply cleanly: $patch" }
        git -C $repo apply -- $patch
        if ($LASTEXITCODE -ne 0) { throw "Unable to apply MikroMCP release patch: $patch" }
        $applied += $patch
    }

    if (-not $SkipNpmInstall) {
        npm.cmd ci --prefix $repo
        if ($LASTEXITCODE -ne 0) { throw "npm ci failed for MikroMCP." }
    }
    npm.cmd test --prefix $repo
    if ($LASTEXITCODE -ne 0) { throw "MikroMCP tests failed." }
    npm.cmd audit --prefix $repo --omit=dev --audit-level=high
    if ($LASTEXITCODE -ne 0) { throw "MikroMCP npm audit failed." }
}
finally {
    for ($index = $applied.Count - 1; $index -ge 0; $index--) {
        $patch = $applied[$index]
        git -C $repo apply --reverse -- $patch
        if ($LASTEXITCODE -ne 0) {
            throw "MikroMCP release patch could not be reverted; inspect the submodule before continuing."
        }
    }
}
