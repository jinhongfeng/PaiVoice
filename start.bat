@echo off
rem PaiVoice 启动器（Windows 双击即可，绕过执行策略）
cd /d "%~dp0"
where pwsh >nul 2>nul
if %errorlevel%==0 (
  pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\run.ps1"
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\run.ps1"
)
pause
