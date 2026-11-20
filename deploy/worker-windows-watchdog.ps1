# Worker 保活看门狗（Windows / 计划任务）
#
# 用法（当前用户即可）：
#   1) 改下面 $Repo、$BaseUrl、$WorkerId
#   2) 装一次：【powershell -NoProfile -ExecutionPolicy Bypass -File deploy\worker-windows-watchdog.ps1 -Install】
#   3) 看它干了什么：Get-Content "$Repo\run\watchdog.log" -Tail 20
#
# 干什么：计划任务每分钟唤起它一次，每次只做两件事
#   - 平台 health 不通 → 拉起 MW-Platform
#   - workbuddy 不在线 → 拉起 MW-Worker
#
# 【Batch17 重要修复】本脚本运行时**绝不**重新注册计划任务。
#   旧版每次运行末尾都用 Register-ScheduledTask -Force 重建 MW-Watchdog，
#   等价于每分钟把任务重建一遍：主体被重置成"仅当用户登录时"、触发器重置到"现在 +1 分钟"，
#   再叠加"每分钟新建一个 powershell 控制台"，就是桌面上不停闪窗口的根源。
#   现在注册只发生在显式 -Install 时，而且注册动作指向**隐藏 VBS 启动器**，
#   根本不创建控制台窗口。
#
# 为什么要隐藏 VBS：powershell.exe 是控制台程序，计划任务在"仅当用户登录时"运行时会先
# 建一个控制台窗口，-WindowStyle Hidden 只是事后把窗口隐藏 → 闪一下。wscript 用
# WScript.Shell.Run(cmd, 0, True) 启动子进程时，窗口根本不会被创建。

param(
    [switch]$Install
)

$ErrorActionPreference = "SilentlyContinue"

$Repo     = "C:\Users\longf\codex-workspace\master-worker-platform"
$BaseUrl  = "http://127.0.0.1:8000"
$WorkerId = "workbuddy"
$TaskName = "MW-Watchdog"

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

# ---- 3) 仅在 -Install 时注册（写隐藏 VBS 启动器 + 每分钟重复）----
if ($Install) {
    $Vbs = Join-Path $Repo "run\launch-watchdog-hidden.vbs"
    $VbsBody = @"
' 无窗口启动 watchdog（Run 第二参数 0 = 不创建控制台窗口）
Dim sh, cmd
cmd = "powershell -NoProfile -ExecutionPolicy Bypass -File ""$Repo\deploy\worker-windows-watchdog.ps1"" >> ""$LogDir\watchdog.out.log"" 2>&1"
Set sh = CreateObject("WScript.Shell")
sh.Run cmd, 0, True
"@
    [System.IO.File]::WriteAllText($Vbs, $VbsBody, (New-Object System.Text.UTF8Encoding($false)))

    $Action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "//nologo `"$Vbs`"" -WorkingDirectory $Repo
    $Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Minutes 1) `
        -RepetitionDuration (New-TimeSpan -Days 3650)
    $Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
    Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger `
        -Settings $Settings -RunLevel Limited -Force | Out-Null
    Write-Host "已注册计划任务 $TaskName（每分钟，隐藏 VBS 启动，无窗口）"
}
