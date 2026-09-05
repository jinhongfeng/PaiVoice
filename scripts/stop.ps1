# stop.ps1 — 停止本机 PaiVoice 全链路（通话服务 :8780 + 语音边车 :8792）
foreach ($port in 8780, 8792) {
  $conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
  if ($conn) {
    $processIds = $conn | Select-Object -ExpandProperty OwningProcess -Unique
    foreach ($processId in $processIds) {
      Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
      Write-Host "[stop] port $port stopped (pid $processId)"
    }
  } else {
    Write-Host "[stop] port $port not running"
  }
}
