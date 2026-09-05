# ============================================================
# PaiVoice 启动脚本（Windows）
# 依次拉起：本地语音边车(local_voice.py :8792) → 通话服务(server.py :8780)
# 用法:  ./scripts/run.ps1          前台运行（Ctrl+C 停止）
#        ./scripts/run.ps1 -Bg      后台运行（配合 scripts/stop.ps1 停止）
# ============================================================
param([switch]$Bg)
$ErrorActionPreference = "Stop"
$here = (Get-Item $PSScriptRoot).Parent.FullName

# 清理旧服务，避免不同监听地址上的多个进程同时提供新旧版本页面。
& (Join-Path $PSScriptRoot "stop.ps1")
Start-Sleep -Milliseconds 500

# 1) 加载 .env 到当前进程环境（必须显式 UTF-8，否则 Get-Content 按 GBK 解码中文会乱码）
$envFile = Join-Path $here ".env"
if (Test-Path $envFile) {
  $lines = [System.IO.File]::ReadAllLines($envFile, [System.Text.Encoding]::UTF8)
  foreach ($line in $lines) {
    $line = $line.Trim()
    if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
      $kv = $line -split "=", 2
      $val = $kv[1].Trim()
      if ($val -match '^(.*?)\s+#') { $val = $matches[1].Trim() }   # 剥离行内注释
      [Environment]::SetEnvironmentVariable($kv[0].Trim(), $val, "Process")
    }
  }
  Write-Host "[run] .env loaded (UTF-8)"
} else {
  Write-Host "[run] WARNING: .env not found, using defaults" -ForegroundColor Yellow
}

$py = Join-Path $here ".venv\Scripts\python.exe"
$voice = Join-Path $here "packages\local-voice\local_voice.py"
$srv = Join-Path $here "packages\realtime-core\server.py"
Set-Location $here

if ($Bg) {
  New-Item -ItemType Directory -Force -Path (Join-Path $here "logs") | Out-Null
  $out = Join-Path $here "logs\server.out.log"
  $err = Join-Path $here "logs\server.err.log"
  Start-Process -FilePath $py -ArgumentList $voice -WorkingDirectory $here -WindowStyle Hidden
  Start-Sleep -Seconds 2
  Start-Process -FilePath $py -ArgumentList $srv -WorkingDirectory $here -RedirectStandardOutput $out -RedirectStandardError $err -WindowStyle Hidden
  Write-Host "[run] local-voice + pai-voice started in background, log: $out"
  Write-Host "[run] dial page: http://localhost:8780/  (停止: ./scripts/stop.ps1)"
} else {
  Write-Host "[run] starting local voice sidecar ..."
  Start-Process -FilePath $py -ArgumentList $voice -WorkingDirectory $here -WindowStyle Hidden
  Start-Sleep -Seconds 2
  & $py $srv
}
