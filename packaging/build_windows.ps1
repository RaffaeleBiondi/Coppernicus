# Build the packaged Windows app (x64):
#
#   packaging\build_windows.ps1
#
# Output in dist\ (git-ignored): the folder Coppernicus\ (Coppernicus.exe for the app,
# coppernicus-cli.exe for the command line and the MCP server, examples\, LICENSE.txt,
# THIRD_PARTY_NOTICES.txt, INSTALL.md), Coppernicus-<version>-windows-x64.zip, and
# Coppernicus-<version>-windows-x64-contents.txt (every file and module of the bundle).
#
# Same steps as build_macos.sh: a build environment of its own (build\venv-windows: uv.lock's
# packages without the dev tools and without triangle, with coppermesh, the rust mesher, built
# by maturin from tools\mesher_rs: uv's "rust" group, it needs a Rust toolchain with MSVC; on a
# uv-managed Python), PyInstaller, packaging\check_bundle.py (fails if triangle is in the bundle
# or coppermesh is not), tools\mesher_rs\check_fma.py (fails on a fused multiply-add in the
# bundled coppermesh), then the zip. Not signed.
# UNTESTED on a Windows computer so far: it runs in .github\workflows\release.yml.
$ErrorActionPreference = "Stop"

function Invoke-Checked {
    # Run a program; stop the build when it fails (PowerShell does not, for programs).
    $program, $arguments = $args
    & $program @arguments
    if ($LASTEXITCODE -ne 0) { throw "failed with exit code ${LASTEXITCODE}: $args" }
}

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Version = (Select-String -Path src\coppernicus\__init__.py -Pattern '^__version__ = "(.*)"').Matches[0].Groups[1].Value
$Name = "Coppernicus-$Version-windows-x64"
$env:UV_PROJECT_ENVIRONMENT = Join-Path $Root "build\venv-windows"
$env:UV_PYTHON_PREFERENCE = "only-managed"
$Py = Join-Path $env:UV_PROJECT_ENVIRONMENT "Scripts\python.exe"

Write-Host "== build environment ($env:UV_PROJECT_ENVIRONMENT)"
if (-not (Get-Command cargo -ErrorAction SilentlyContinue)) { throw "no Rust toolchain (cargo) on PATH: coppermesh is built from source" }
Invoke-Checked uv sync --frozen --no-dev --group rust --no-install-package triangle --python 3.12
Invoke-Checked uv pip install --python $Py -r packaging\requirements-build.txt
& $Py -c "import importlib.util, sys; sys.exit(importlib.util.find_spec('triangle') is not None)"
if ($LASTEXITCODE -ne 0) { throw "triangle is installed in the build environment" }
Invoke-Checked $Py -c "import coppermesh"

Write-Host "== PyInstaller"
if (Test-Path dist\Coppernicus) { Remove-Item -Recurse -Force dist\Coppernicus }
Invoke-Checked $Py -m PyInstaller --noconfirm --clean --distpath dist --workpath build\pyinstaller packaging\coppernicus.spec

Write-Host "== bundle check"
Invoke-Checked $Py packaging\check_bundle.py dist\Coppernicus --list "dist\$Name-contents.txt"
$Rust = @(Get-ChildItem -Recurse -Path dist\Coppernicus -Filter "coppermesh*.pyd" | Where-Object { $_.Directory.Name -eq "coppermesh" })
if ($Rust.Count -ne 1) { throw "expected one coppermesh .pyd in the bundle, found $($Rust.Count)" }
Invoke-Checked $Py tools\mesher_rs\check_fma.py $Rust[0].FullName

Write-Host "== zip"
Copy-Item -Recurse examples dist\Coppernicus\examples
Remove-Item dist\Coppernicus\examples\make_examples.py
Copy-Item LICENSE dist\Coppernicus\LICENSE.txt
Copy-Item build\packaging\THIRD_PARTY_NOTICES.txt dist\Coppernicus\THIRD_PARTY_NOTICES.txt
Copy-Item -Recurse packaging\licenses dist\Coppernicus\licenses     # the notices refer to licenses\
Copy-Item docs\install.md dist\Coppernicus\INSTALL.md
if (Test-Path "dist\$Name.zip") { Remove-Item "dist\$Name.zip" }
Invoke-Checked $Py -c "import shutil, sys; shutil.make_archive(sys.argv[1], 'zip', 'dist', 'Coppernicus')" "dist\$Name"
$size = [math]::Round((Get-Item "dist\$Name.zip").Length / 1MB, 1)
Write-Host "Built dist\$Name.zip ($size MB)"
