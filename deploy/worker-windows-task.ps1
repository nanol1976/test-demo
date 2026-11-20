# Master-Worker Worker 守护脚本（Windows / 计划任务）
#
# 用法（当前用户即可，不需要管理员）：
#   1) 改下面 $Repo、$BaseUrl、$WorkerId、$Token 四个变量
#   2) powershell -NoProfile -ExecutionPolicy Bypass -File deploy\worker-windows-task.ps1
#   3) 看状态：Get-ScheduledTask -TaskName "MW-Worker" | Get-ScheduledTaskInfo
#      看日志：Get-Content "$Repo\run\worker.out.log" -Tail 50 -Wait
#
# 设计要点（都是踩过坑之后定下来的）：
#   - **不直接让计划任务执行 python.exe**：本脚本生成并注册 run\worker-run.bat，
#     由 bat 设 PYTHONPATH、重定向日志、把退出码透传出来。否则日志重定向和退出码都不可靠，
#     "崩了能不能被拉起"根本没法判断。
#   - **登录触发 + 每分钟重复**：任务在跑时 MultipleInstances=IgnoreNew 直接忽略，
#     等于每分钟做一次"它还在吗"的检查。比单纯依赖"失败重启"可靠（强杀时不一定会重启）。
#   - RunLevel Limited：普通用户权限即可注册；AtStartup + Highest 需要管理员，现场常常没有。
#   - 停止任务时 Worker 侧 --grace-seconds 30 负责把在跑任务收尾再退出。

$ErrorActionPreference = "Stop"

$Repo     = "C:\Users\longf\codex-workspace\master-worker-platform"
$BaseUrl  = "http://127.0.0.1:8000"
$WorkerId = "workbuddy"
$Token    = ""          # 平台开了鉴权才填

$TaskName = "MW-Worker"
$LogDir   = Join-Path $Repo "run"
$LogFile  = Join-Path $LogDir "worker.out.log"
$Script   = Join-Path $Repo "worker\agent_worker.py"
$Python   = Join-Path $Repo ".venv311\Scripts\python.exe"
$BatPath  = Join-Path $LogDir "worker-run.bat"

if (-not (Test-Path $Python)) { throw "找不到 Python：$Python" }
if (-not (Test-Path $Script)) { throw "找不到 Worker 脚本：$Script" }
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$ArgList = @(
    "`"$Script`"",
    "--base-url", $BaseUrl,
    "--worker-id", $WorkerId,
    "--concurrency", "2",
    "--min-concurrency", "1",
    "--grace-seconds", "30",
    "--registry-interval", "15",
    "--status-file", "`"$LogDir\worker_status.json`"",
    "--state-file", "`"$LogDir\worker_done.json`"",
    "--allow-root", "`"$Repo`"",
    "--work-dir", "`"$Repo`""
)
if ($Token -ne "") { $ArgList += @("--token", $Token) }

$BatLines = @(
    "@echo off",
    "chcp 65001 >nul",
    "cd /d $Repo",
    "set PYTHONPATH=$Repo",
    ('"' + $Python + '" ' + ($ArgList -join " ") + ' >> "' + $LogFile + '" 2>&1'),
    "set RC=%ERRORLEVEL%",
    ('echo EXIT=%RC% >> "' + $LogFile + '"'),
    "exit /b %RC%"
)
[System.IO.File]::WriteAllLines($BatPath, $BatLines, (New-Object System.Text.UTF8Encoding($false)))

$Action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c $BatPath" -WorkingDirectory $Repo
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$Trigger.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 1) `
    -RepetitionDuration (New-TimeSpan -Days 3650)).Repetition
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 0)

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger `
    -Settings $Settings -RunLevel Limited -Force | Out-Null

Write-Host "已注册计划任务 $TaskName"
Write-Host "启动：Start-ScheduledTask -TaskName $TaskName"
Write-Host "查看：Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo"
Write-Host "日志：$LogFile"
Write-Host "提示：再跑一次 deploy\worker-windows-watchdog.ps1 可以让平台/Worker 掉了自动拉起"
