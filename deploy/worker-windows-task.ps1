# Master-Worker Worker 守护脚本（Windows / 计划任务）
#
# 用法（在管理员 PowerShell 里跑一次）：
#   1) 改下面 $Repo、$Python、$BaseUrl、$WorkerId、$Token 五个变量
#   2) .\deploy\worker-windows-task.ps1
#   3) 看状态：Get-ScheduledTask -TaskName "MW-Worker" | Get-ScheduledTaskInfo
#      看日志：Get-Content "$Repo\run\worker.out.log" -Tail 50 -Wait
#
# 设计要点：
#   - 计划任务以「开机启动」触发，多个实例被禁止（MultipleInstances=IgnoreNew）
#   - 任务失败后每 1 分钟重试，最多 999 次（等于常驻自愈）
#   - 停止任务时 PowerShell 会发 CloseMainWindow/终止；Worker 侧 --grace-seconds 30 保证收尾
#   - 输出重定向到 run\worker.out.log，便于事后排查

$ErrorActionPreference = "Stop"

$Repo     = "C:\Users\longf\codex-workspace\master-worker-platform"
$Python   = Join-Path $Repo ".venv311\Scripts\python.exe"
$BaseUrl  = "http://127.0.0.1:8000"
$WorkerId = "workbuddy"
$Token    = ""          # 平台开了鉴权才填

$TaskName = "MW-Worker"
$LogDir   = Join-Path $Repo "run"
$LogFile  = Join-Path $LogDir "worker.out.log"
$Script   = Join-Path $Repo "worker\agent_worker.py"

if (-not (Test-Path $Python))   { throw "找不到 Python：$Python" }
if (-not (Test-Path $Script))   { throw "找不到 Worker 脚本：$Script" }
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$ArgList = @(
    "`"$Script`"",
    "--base-url", $BaseUrl,
    "--worker-id", $WorkerId,
    "--concurrency", "2",
    "--grace-seconds", "30",
    "--status-file", "`"$LogDir\worker_status.json`"",
    "--state-file", "`"$LogDir\worker_done.json`""
)
if ($Token -ne "") { $ArgList += @("--token", $Token) }

# 用 cmd 套一层，把 stdout/stderr 都重定向（Python 自己不解引用时也能留日志）
$CmdLine = "`"$Python`" " + ($ArgList -join " ") + " >> `"$LogFile`" 2>&1"

$Action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c $CmdLine" -WorkingDirectory $Repo
$Trigger = New-ScheduledTaskTrigger -AtStartup
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 0)

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger `
    -Settings $Settings -RunLevel Highest -Force | Out-Null

Write-Host "已注册计划任务 $TaskName"
Write-Host "启动：Start-ScheduledTask -TaskName $TaskName"
Write-Host "查看：Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo"
Write-Host "日志：$LogFile"
