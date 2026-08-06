# Build "YouTube Recorder" (Windows) · By Leoluchino
#
# PyInstaller onedir build, sibling of build_app.sh (the macOS one). No
# installer yet — output is a portable folder, unzip anywhere and run
# "YouTube Recorder.exe". Assumes dependencies are already installed in
# whatever `python` is first on PATH (this repo's CI workflow does that
# with `pip install -e ".[pipeline,ai,dossier]" pywebview`; rumps/pyobjc
# are macOS-only and deliberately NOT installed here).
#
# Usage (from repo root or anywhere):
#   pwsh scripts/build_app_windows.ps1
# or, since this lives at app/scripts/, from a checkout root:
#   pwsh app/scripts/build_app_windows.ps1

$ErrorActionPreference = "Stop"

$AppDir = Split-Path -Parent $PSScriptRoot   # .../app
$Proj = Split-Path -Parent $AppDir           # repo root (sibling of app/)

Set-Location $AppDir
Remove-Item -Recurse -Force "dist","build" -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force (Join-Path $Proj "YouTube Recorder") -ErrorAction SilentlyContinue

# --workpath outside app/ so intermediate build artifacts never land inside
# the repo tree (mirrors build_app.sh's `mktemp -d` workpath on macOS).
$WorkPath = Join-Path $env:TEMP ("ytrec-build-" + [guid]::NewGuid())
python -m PyInstaller scripts/pyinstaller_windows.spec `
    --distpath "$Proj" --workpath "$WorkPath" --noconfirm

Write-Host "built: $Proj\YouTube Recorder\YouTube Recorder.exe"
