[CmdletBinding()]
param(
    [string]$BlenderPath = "blender"
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$dist = Join-Path $root "dist"
New-Item -ItemType Directory -Path $dist -Force | Out-Null

$manifestPath = Join-Path $root "blender_manifest.toml"
$manifest = Get-Content -LiteralPath $manifestPath -Raw

function Get-ManifestValue {
    param([string]$Name)

    $match = [regex]::Match(
        $manifest,
        "(?m)^$([regex]::Escape($Name))\s*=\s*`"([^`"]+)`"\s*$"
    )
    if (-not $match.Success) {
        throw "Missing '$Name' in $manifestPath."
    }
    return $match.Groups[1].Value
}

$packageId = Get-ManifestValue "id"
$version = Get-ManifestValue "version"
$outputPath = Join-Path $dist "$packageId-$version.zip"
if (Test-Path -LiteralPath $outputPath) {
    throw "Package already exists: $outputPath"
}

& $BlenderPath --command extension build `
    --source-dir $root `
    --output-filepath $outputPath

if ($LASTEXITCODE -ne 0) {
    throw "Blender extension build failed with exit code $LASTEXITCODE."
}
