# Worker 保活看门狗（Windows / 计划任务）
#
# 用法（当前用户即可）：
#   1) 改下面 $Repo、$BaseUrl、$WorkerId
#   2) powershell -NoProfile -ExecutionPolicy Bypass -File deploy\worker-windows-watchdog.ps1
#   3) 看它干了什么：Get-Content "$Repo\run\watchdog.log" -Tail 20
#
# 干什么：每 1 分钟检查一次
#   - 平台 health 不通 → 拉起 MW-Platform
#   - workbuddy 不在线 → 拉起 MW-Worker
#
# 为什么要它（实测结论）：
#   Windows 计划任务的"失败后重启"在进程被强杀（Stop-Process / 任务管理器结束）时并不一定触发；
#   "每分钟重复 + MultipleInstances=IgnoreNew"则是稳定可靠的：任务在跑时重复触发被忽略，
#   掉线后下一分钟必然被拉起。看门狗就是这个兜底。

$ErrorActionPreference = "SilentlyContinue"

$Repo     = "C:\Users\longf\codex-workspace\master-worker-platform"
$BaseUrl  = "http://127.0.0.1:8000"
$WorkerId = "workbuddy"

$LogDir = Join-Path $Repo "run"
$Log    = Join-Path $LogDir "watchdog.log"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
function Note($m) { Add-Content -Path $Log -Value ((Get-Date).ToString("s") + " " + $m) }

try { Invoke-WebRequest -Uri "$BaseUrl/api/health" -UseBasicParsing -TimeoutSec 5 | Out-Null }
catch {
    Note "platform down -> start MW-Platform"
    Start-ScheduledTask -TaskName "MW-Platform"
    Start-Sleep -Seconds 8
}

try { $w = Invoke-RestMethod -Uri "$BaseUrl/api/workers" -TimeoutSec 5 } catch { $w = @() }
$alive = @($w | Where-Object { $_.worker_id -eq $WorkerId -and $_.online })
if ($alive.Count -eq 0) {
    Note "worker not online -> start MW-Worker"
    Start-ScheduledTask -TaskName "MW-Worker"
}

# —— 顺手把自己注册成每分钟跑一次的任务（重复执行本脚本是幂等的）——
$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 1) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
$Action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$PSCommandPath`"" `
    -WorkingDirectory $Repo
Register-ScheduledTask -TaskName "MW-Watchdog" -Action $Action -Trigger $Trigger -Settings $Settings `
    -RunLevel Limited -Force | Out-Null

Write-Host "已注册计划任务 MW-Watchdog（每 1 分钟检查一次）"
