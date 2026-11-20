# Master-Worker Worker 守护脚本（Windows / 计划任务）—— 兼容入口
#
# 【Batch23】注册逻辑已统一到 deploy\install-windows-tasks.ps1。
#   本文件保留只是因为老文档/老习惯会敲这个文件名，内部直接转发，不再自己注册，
#   避免"两份实现各自演化、改一处漏一处"。
#
# 用法（普通用户即可，不需要管理员）：
#   powershell -NoProfile -ExecutionPolicy Bypass -File deploy\install-windows-tasks.ps1 -StartTasks
#   或（等价）：
#   powershell -NoProfile -ExecutionPolicy Bypass -File deploy\worker-windows-task.ps1
#
# 装完之后：
#   看状态：Get-ScheduledTask -TaskName 'MW-*' | Format-Table TaskName, State
#   看日志：Get-Content "$Repo\run\worker.out.log" -Tail 50

param(
    [string]$Repo = "",
    [string]$BaseUrl = "http://127.0.0.1:8000",
    [string]$WorkerId = "workbuddy",
    [int]$Concurrency = 2,
    [string]$Token = ""
)

$ErrorActionPreference = "Stop"
$installer = Join-Path $PSScriptRoot "install-windows-tasks.ps1"
if (-not (Test-Path $installer)) { throw "缺少 $installer" }

# 端口从 BaseUrl 里取（老入口给人的是 URL，不是端口号）
$Port = 8000
if ($BaseUrl -match ':(\d+)') { $Port = [int]$Matches[1] }

Write-Host "→ 转发到 install-windows-tasks.ps1（三个任务一起注册，幂等）"
& $installer -Repo $Repo -WorkerId $WorkerId -Concurrency $Concurrency -Token $Token -Port $Port
