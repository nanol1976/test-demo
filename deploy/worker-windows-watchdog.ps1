# Worker 保活看门狗（Windows / 计划任务）
#
# 用法（当前用户即可）：
#   装一次：powershell -NoProfile -ExecutionPolicy Bypass -File deploy\install-windows-tasks.ps1 -StartTasks
#   看它干了什么：Get-Content "$Repo\run\watchdog.log" -Tail 20
#
# 干什么：计划任务每分钟唤起它一次，每次只做两件事
#   - 平台 health 不通 → 拉起 MW-Platform
#   - workbuddy 不在线 → 拉起 MW-Worker
#
# 【Batch17 重要修复】本脚本运行时**绝不**重新注册计划任务。
#   旧版每次运行末尾都用 Register-ScheduledTask -Force 重建 MW-Watchdog，
#   等价于每分钟把任务重建一遍：主体被重置成"仅当用户登录时"、触发器重置到"现在 +1 分钟"，
#   再叠加"每分钟新建一个 powershell 控制台"，就是桌面上不停闪窗口的根源。
#
# 【Batch23】注册动作全部收进 deploy\install-windows-tasks.ps1（三处合一）：
#   - 本脚本带 -Install 时不再自己写 VBS、自己注册，直接转发给统一安装脚本；
#   - 任务动作统一是 wscript.exe 跑仓库内的隐藏 VBS（sh.Run cmd, 0, True），根本不建控制台。

param(
    [switch]$Install,
    [string]$Repo = "",
    [string]$BaseUrl = "http://127.0.0.1:8000",
    [string]$WorkerId = "workbuddy"
)

$ErrorActionPreference = "SilentlyContinue"

# 仓库根：默认取本脚本的上一级（deploy 的父目录），不写死用户名
if ([string]::IsNullOrWhiteSpace($Repo)) {
    $Repo = Split-Path -Parent $PSScriptRoot
}

if ($Install) {
    # 兼容老习惯（以前只跑看门狗 -Install）。现在三个任务统一由安装脚本注册
    & (Join-Path $PSScriptRoot "install-windows-tasks.ps1") -Repo $Repo
    exit $LASTEXITCODE
}

$LogDir = Join-Path $Repo "run"
$Log    = Join-Path $LogDir "watchdog.log"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
function Note($m) { Add-Content -Path $Log -Value ((Get-Date).ToString("s") + " " + $m) }

# ---- 1) 平台在不在 ----
try { Invoke-WebRequest -Uri "$BaseUrl/api/health" -UseBasicParsing -TimeoutSec 5 | Out-Null }
catch {
    Note "platform down -> start MW-Platform"
    Start-ScheduledTask -TaskName "MW-Platform"
    Start-Sleep -Seconds 8
}

# ---- 2) Worker 在不在线 ----
try { $w = Invoke-RestMethod -Uri "$BaseUrl/api/workers" -TimeoutSec 5 } catch { $w = @() }
$alive = @($w | Where-Object { $_.worker_id -eq $WorkerId -and $_.online })
if ($alive.Count -eq 0) {
    Note "worker not online -> start MW-Worker"
    Start-ScheduledTask -TaskName "MW-Worker"
}
