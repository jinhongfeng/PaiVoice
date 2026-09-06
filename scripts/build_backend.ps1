# ============================================================
# PaiVoice backend build script (PyInstaller)
# Output: dist\server.exe + dist\local_voice.exe (+ _internal)
# Privacy: .env is NOT copied; user data/config stay on dev machine
# Usage:  pwsh -NoProfile -ExecutionPolicy Bypass -File scripts\build_backend.ps1
# ============================================================
$ErrorActionPreference = "Stop"
$here = (Get-Item $PSScriptRoot).Parent.FullName
Set-Location $here

$py = Join-Path $here ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw ".venv not found: create venv and pip install -r requirements.txt first" }

# Install pyinstaller into .venv if missing (does not touch global env)
& $py -m PyInstaller --version *> $null
if ($LASTEXITCODE -ne 0) {
  Write-Host "[build] installing pyinstaller ..."
  & $py -m pip install pyinstaller
  if ($LASTEXITCODE -ne 0) { throw "pyinstaller install failed" }
}

$server = Join-Path $here "packages\realtime-core\server.py"
$voice  = Join-Path $here "packages\local-voice\local_voice.py"
if (-not (Test-Path $server)) { throw "server.py not found: $server" }
if (-not (Test-Path $voice))  { throw "local_voice.py not found: $voice" }

# Clean old outputs to avoid stale files mixing in
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue (Join-Path $here "build")
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue (Join-Path $here "dist")

# onefile each; --noconsole (background service, no console window); --icon 给 exe 文件本身上图标
$icon = Join-Path $here "assets\icons\paivoice-icon.ico"
& $py -m PyInstaller --onefile --noconsole --icon $icon --name server.exe $server
if ($LASTEXITCODE -ne 0) { throw "server.exe build failed" }
& $py -m PyInstaller --onefile --noconsole --icon $icon --name local_voice.exe $voice
if ($LASTEXITCODE -ne 0) { throw "local_voice.exe build failed" }

Write-Host "[build] OK -> dist\server.exe + dist\local_voice.exe"
Write-Host "[build] Note: .env is NOT bundled. Enter keys in the settings panel on first run (stored under %APPDATA%\PaiVoice)."
