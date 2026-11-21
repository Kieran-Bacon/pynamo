<#
.SYNOPSIS
    Clear the dist folder, build the package and publish it to PyPI.

.DESCRIPTION
    Any arguments are passed to `uv publish`, e.g. `--dry-run` to check the upload without
    sending it, or `--token <token>`. Without a token, uv reads UV_PUBLISH_TOKEN.

.EXAMPLE
    .\bin\publish.ps1 --dry-run
#>

$ErrorActionPreference = "Stop"

# Run from the project root, wherever the script is called from
Set-Location (Split-Path $PSScriptRoot -Parent)

# Remove old builds, so only this version's files are uploaded
if (Test-Path dist) {
    Remove-Item -Recurse -Force dist
}

# Build the sdist and wheel into dist, stopping if the build fails
uv build
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

# Upload everything in dist, passing on any extra arguments
uv publish @args
exit $LASTEXITCODE
