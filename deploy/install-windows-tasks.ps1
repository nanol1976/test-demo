# 一键注册 Windows 计划任务（Batch23 建三个，Batch25 加 MW-Guard）
#
# 用法（普通用户即可，不需要管理员）：
#   powershell -NoProfile -ExecutionPolicy Bypass -File deploy\install-windows-tasks.ps1
#   只想看会做什么：加 -DryRun
#   注册完立刻拉起来：加 -StartTasks
#   全部拆掉：-Uninstall
#
# 为什么要有这个脚本：
#   Batch17 之后三个任务的动作都改成了 wscript.exe 跑隐藏 VBS（不建控制台，才不闪窗），
#   但注册脚本只存在于"某台机器的 run\ 或手敲的命令历史"里——换一台机器、换一个人，
#   根本不知道 MW-Platform 该怎么注册。这里把四件事收进一个仓库内的脚本：
#     1) 生成 run\worker-run.bat（worker 的命令行只有这一份真相）
#     2) 注册 MW-Platform / MW-Worker / MW-Watchdog，动作统一 wscript + 隐藏 VBS
#     3) 注册 MW-Guard（Batch25）：explorer 现场快照 + 死了就拉起 + 每天一次 data/ 巡检
#     4) 四个任务都是「登录触发 + 每分钟重复 + IgnoreNew」，等于常驻自愈
#
# 幂等：重复跑会 -Force 覆盖同名任务，不会堆出重复任务。
# 不做的事：不猜 python 路径（用仓库内的 .venv311），不动数据库，不杀进程。
#
# 卸载（-Uninstall）只注销任务，不删仓库里的任何文件。

param(
    [switch]$Uninstall,
    [switch]$StartTasks,
    [switch]$DryRun,
    [string]$Repo = "",
    [string]$WorkerId = "workbuddy",
    [int]$Concurrency = 2,
    [int]$Port = 8000,
    [string]$Token = ""
)

$ErrorActionPreference = "Stop"

# 仓库根：默认是本脚本的上一级（deploy 的父目录），不写死用户名
if ([string]::IsNullOrWhiteSpace($Repo)) {
    $Repo = Split-Path -Parent $PSScriptRoot
}
$Repo = (Resolve-Path $Repo).Path

$LogDir  = Join-Path $Repo "run"
$Python  = Join-Path $Repo ".venv311\Scripts\python.exe"
$Worker  = Join-Path $Repo "worker\agent_worker.py"
$Bat     = Join-Path $LogDir "worker-run.bat"
$LogFile = Join-Path $LogDir "worker.out.log"
$BaseUrl = "http://127.0.0.1:$Port"

$Tasks = @(
    @{ Name = "MW-Platform"; Vbs = (Join-Path $Repo "deploy\launch-platform-hidden.vbs") },
    @{ Name = "MW-Worker";   Vbs = (Join-Path $Repo "deploy\launch-worker-hidden.vbs") },
    @{ Name = "MW-Watchdog"; Vbs = (Join-Path $Repo "deploy\launch-watchdog-hidden.vbs") },
    @{ Name = "MW-Guard";    Vbs = (Join-Path $Repo "deploy\launch-guard-hidden.vbs") }
)
# Batch30：每日快照不是常驻进程，单独一条 Daily 任务（默认 00:05），动作同样是 wscript 跑隐藏 VBS
$SnapshotTask = @{ Name = "MW-Snapshot"; Vbs = (Join-Path $Repo "deploy\launch-snapshot-hidden.vbs") }
$DailyAt = "00:05"
# 预检/卸载要覆盖全部 5 条（含快照），注册仍按常驻 4 条 + 每日 1 条分别处理
$AllTasks = $Tasks + $SnapshotTask

function Say($m) { Write-Host $m }

# ---------- 卸载 ----------
if ($Uninstall) {
    foreach ($t in $AllTasks) {
        $old = Get-ScheduledTask -TaskName $t.Name -ErrorAction SilentlyContinue
        if ($null -eq $old) { Say "未注册：$($t.Name)"; continue }
        if ($DryRun) { Say "将注销：$($t.Name)"; continue }
        Unregister-ScheduledTask -TaskName $t.Name -Confirm:$false
        Say "已注销：$($t.Name)"
    }
    return
}

# ---------- 预检 ----------
foreach ($t in $AllTasks) {
    if (-not (Test-Path $t.Vbs)) { throw "缺少启动器：$($t.Vbs)" }
}
if (-not (Test-Path $Python)) { throw "找不到 Python：$Python（先按文档建 .venv311）" }
if (-not (Test-Path $Worker)) { throw "找不到 Worker 脚本：$Worker" }

if (-not $DryRun) {
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
}

# ---------- 0) 鉴权 token：没显式给就从仓库 .env 里取（Batch27） ----------
# 平台一开 API_TOKEN，Worker 不带同一个就会被 401 打回；token 不写进命令行以外的地方，
# 也不打印到控制台（避免日志里留明文）。
if ([string]::IsNullOrWhiteSpace($Token)) {
    $EnvFile = Join-Path $Repo ".env"
    if (Test-Path -LiteralPath $EnvFile) {
        foreach ($line in (Get-Content -LiteralPath $EnvFile -Encoding UTF8)) {
            if ($line -match '^\s*API_TOKEN\s*=\s*(.+?)\s*$') {
                $Token = $Matches[1].Trim().Trim('"').Trim("'")
            }
        }
    }
}
if ($Token -ne "") { Say "已从 .env 取到 API_TOKEN（长度 $($Token.Length)，不打印内容）" }

# ---------- 1) 生成 worker-run.bat ----------
$ArgList = @(
    "`"$Worker`"",
    "--base-url", $BaseUrl,
    "--worker-id", $WorkerId,
    "--concurrency", "$Concurrency",
    "--grace-seconds", "30",
    "--status-file", "`"$LogDir\worker_status.json`"",
    "--state-file", "`"$LogDir\worker_done.json`"",
    "--registry-interval", "15",
    "--allow-root", "`"$Repo`"",
    "--work-dir", "`"$Repo`""
)
if ($Token -ne "") { $ArgList += @("--token", $Token) }

$BatLines = @(
    "@echo off",
    "chcp 65001 >nul",
    "cd /d `"$Repo`"",
    "set PYTHONPATH=$Repo",
    ('"' + $Python + '" ' + ($ArgList -join " ") + ' >> "' + $LogFile + '" 2>&1'),
    "set RC=%ERRORLEVEL%",
    ('echo EXIT=%RC% >> "' + $LogFile + '"'),
    "exit /b %RC%"
)
if ($DryRun) {
    Say "将生成：$Bat（$($BatLines.Count) 行）"
} else {
    [System.IO.File]::WriteAllLines($Bat, $BatLines, (New-Object System.Text.UTF8Encoding($false)))
    Say "已生成：$Bat"
}

# ---------- 2) 注册四个任务 ----------
# 动作只有一份真相：常驻任务与每日快照都从这里取（verify_b23 会数 wscript.exe 出现次数）
function New-MwAction($Vbs) {
    return New-ScheduledTaskAction -Execute "wscript.exe" `
        -Argument "//nologo `"$Vbs`"" -WorkingDirectory $Repo
}

function New-MwTask($Name, $Vbs) {
    $Action = New-MwAction $Vbs
    $Trigger = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(1)) `
        -RepetitionInterval (New-TimeSpan -Minutes 1) `
        -RepetitionDuration (New-TimeSpan -Days 3650)
    $Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
    return @{ Action = $Action; Trigger = $Trigger; Settings = $Settings }
}

foreach ($t in $Tasks) {
    if ($DryRun) {
        Say ("将注册：" + $t.Name + " -> wscript.exe //nologo `"" + $t.Vbs + "`"")
        continue
    }
    $parts = New-MwTask $t.Name $t.Vbs
    Register-ScheduledTask -TaskName $t.Name -Action $parts.Action -Trigger $parts.Trigger `
        -Settings $parts.Settings -RunLevel Limited -Force | Out-Null
    Say ("已注册：" + $t.Name)
}

# ---------- 2b) 每日快照（Batch30）：Daily 触发，跑完即结束，不重复拉起、不重启 ----------
if ($DryRun) {
    Say ("将注册：" + $SnapshotTask.Name + " -> 每天 " + $DailyAt + " 跑一次 daily_snapshot.py（输出 data\snapshots）")
} else {
    $SnapAction = New-MwAction $SnapshotTask.Vbs
    $SnapTrigger = New-ScheduledTaskTrigger -Daily -At $DailyAt
    $SnapSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
    Register-ScheduledTask -TaskName $SnapshotTask.Name -Action $SnapAction -Trigger $SnapTrigger `
        -Settings $SnapSettings -RunLevel Limited -Force | Out-Null
    Say ("已注册：" + $SnapshotTask.Name + "（每天 " + $DailyAt + "）")
}

# ---------- 3) 可选：立刻拉起来 ----------
if ($StartTasks -and -not $DryRun) {
    foreach ($t in $Tasks) {
        Start-ScheduledTask -TaskName $t.Name
        Say ("已启动：" + $t.Name)
    }
}

if (-not $DryRun) {
    Say ""
    Say "核对：Get-ScheduledTask -TaskName 'MW-*' | Format-Table TaskName, State"
    Say "       Get-ScheduledTask -TaskName 'MW-*' | Get-ScheduledTaskInfo"
    Say "日志：$LogDir\platform.out.log / worker.out.log / watchdog.log / explorer-guard.log"
    Say ("看板：http://127.0.0.1:$Port/")
}
