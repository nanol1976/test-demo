# Explorer 守护 + data/ 卫生巡检（Batch25）
#
# 为什么要有它：
#   这台机器 7 天里 explorer.exe（桌面外壳）"意外停止"了 10 次，11-20 14:15 那次
#   直接演变成整机无响应，只能硬重启。崩溃报告里没有出错模块，也没有磁盘/驱动告警，
#   单靠事件日志定不了因。所以这里做两件事：
#     1) 每 2 分钟留一份现场快照（下次卡死，能倒着看它死之前是什么状态）；
#     2) explorer 不在就拉起来；连续两次超阈值就软重启它（不加 -Restart 则只记录）。
#   顺带把 Batch24 定的 data/ 卫生规则变成每天自动巡检一次（默认只报不删）。
#
# 用法（普通用户即可，不需要管理员）：
#   powershell -NoProfile -ExecutionPolicy Bypass -File deploy\explorer-guard.ps1 -Restart
#   -DryRun        只打印，不写日志、不改状态、不动进程
#   -Restart       允许自动拉起 / 软重启 explorer（不加就只记录）
#   -AutoCleanData 巡检时顺手清掉白名单残留（verify_*.db / smoke.db / b8_*.db / 旧备份）
#   -Uninstall     只注销 MW-Guard 任务，不删任何文件
#
# 不做的事：不删 mvp.db、不杀别的进程、不改 PATH、不碰注册表。

param(
    [switch]$DryRun,
    [switch]$Restart,
    [switch]$AutoCleanData,
    [switch]$Uninstall,
    [int]$MemLimitMB = 1200,
    [int]$HandleLimit = 10000,
    [int]$DataCheckHour = 3,
    [int]$MaxLogLines = 5000,
    [string]$Repo = ""
)

$ErrorActionPreference = "Continue"
$TaskName = "MW-Guard"

# 仓库根：默认是本脚本的上一级（deploy 的父目录），不写死用户名
if ([string]::IsNullOrWhiteSpace($Repo)) {
    $Repo = Split-Path -Parent $PSScriptRoot
}
$Repo = (Resolve-Path $Repo).Path

$LogDir    = Join-Path $Repo "run"
$LogFile   = Join-Path $LogDir "explorer-guard.log"
$StateFile = Join-Path $LogDir "explorer-guard.state.json"
$DataDir   = Join-Path $Repo "data"

function Say($m) { Write-Host $m }

# ---------- 卸载 ----------
if ($Uninstall) {
    $old = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $old) { Say "未注册：$TaskName"; return }
    if ($DryRun) { Say "将注销：$TaskName"; return }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Say "已注销：$TaskName"
    return
}

function Get-Stamp { (Get-Date).ToString("yyyy-MM-dd HH:mm:ss") }

function Write-GuardLog($line) {
    if ($DryRun) { Say ("[DRYRUN] " + $line); return }
    $full = (Get-Stamp) + " " + $line
    Add-Content -LiteralPath $LogFile -Value $full -Encoding UTF8
    Say $full
}

# ---------- 状态（连续超阈值次数 + 上次 data 巡检日期）----------
$state = @{ breach = 0; lastDataCheck = "" }
if ((-not $DryRun) -and (Test-Path -LiteralPath $StateFile)) {
    try {
        $raw = Get-Content -LiteralPath $StateFile -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($null -ne $raw.breach) { $state.breach = [int]$raw.breach }
        if ($null -ne $raw.lastDataCheck) { $state.lastDataCheck = [string]$raw.lastDataCheck }
    } catch {
        $state.breach = 0
        $state.lastDataCheck = ""
    }
}

if (-not $DryRun) {
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
}

# ---------- 1) explorer 现场采样 ----------
$os = Get-CimInstance Win32_OperatingSystem -ErrorAction SilentlyContinue
$freeGB = ""
if ($null -ne $os) { $freeGB = [math]::Round($os.FreePhysicalMemory / 1MB, 1) }

$procs = @(Get-Process explorer -ErrorAction SilentlyContinue | Sort-Object WorkingSet64 -Descending)

if ($procs.Count -eq 0) {
    Write-GuardLog "MISSING explorer 不在（桌面外壳已死）"
    if ($Restart) {
        if (-not $DryRun) { Start-Process explorer.exe -ErrorAction SilentlyContinue }
        Write-GuardLog "ACT explorer 已重新拉起"
    } else {
        Write-GuardLog "SKIP 没有 -Restart，只记录不动手"
    }
    $state.breach = 0
} else {
    $p = $procs[0]
    $memMB = [int]($p.WorkingSet64 / 1MB)
    $privMB = [int]($p.PrivateMemorySize64 / 1MB)
    $handles = 0
    try { $handles = $p.HandleCount } catch { $handles = 0 }
    $threads = 0
    try { $threads = $p.Threads.Count } catch { $threads = 0 }
    $count = $procs.Count

    Write-GuardLog ("SAMPLE explorer 实例=" + $count + " pid=" + $p.Id + " 内存=" + $memMB + "MB 私有=" + $privMB + "MB 句柄=" + $handles + " 线程=" + $threads + " 系统可用=" + $freeGB + "GB")

    if ($count -gt 1) {
        Write-GuardLog ("WARN explorer 有 " + $count + " 个实例（正常情况下只有一个）")
    }

    $over = ($memMB -gt $MemLimitMB) -or ($handles -gt $HandleLimit)
    if ($over) {
        $state.breach = $state.breach + 1
        Write-GuardLog ("WARN 超阈值（内存>" + $MemLimitMB + "MB 或句柄>" + $HandleLimit + "）连续第 " + $state.breach + " 次")
        if ($state.breach -ge 2) {
            if ($Restart) {
                if (-not $DryRun) {
                    Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
                    Start-Sleep -Seconds 3
                    Start-Process explorer.exe -ErrorAction SilentlyContinue
                }
                Write-GuardLog ("ACT explorer 已软重启（原来 内存=" + $memMB + "MB 句柄=" + $handles + "）")
                $state.breach = 0
            } else {
                Write-GuardLog "SKIP 没有 -Restart，只记录不动手"
            }
        }
    } else {
        if ($state.breach -ne 0) { Write-GuardLog "OK 已回落到阈值内，连续计数清零" }
        $state.breach = 0
    }
}

# ---------- 2) data/ 卫生巡检（每天一次，默认只报不删）----------
$today = (Get-Date).ToString("yyyy-MM-dd")
$hourNow = (Get-Date).Hour
if (($hourNow -ge $DataCheckHour) -and ($state.lastDataCheck -ne $today)) {
    if (-not (Test-Path -LiteralPath $DataDir)) {
        Write-GuardLog "DATA 没有 data 目录，跳过"
    } else {
        $dbFiles = @(Get-ChildItem -LiteralPath $DataDir -File -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -like "*.db" -or $_.Name -like "*.db.bak-*" })
        $keep = @("mvp.db")
        $baks = @($dbFiles | Where-Object { $_.Name -like "mvp.db.bak-*" } | Sort-Object LastWriteTime -Descending)
        $keepBak = $null
        if ($baks.Count -gt 0) { $keepBak = $baks[0].Name; $keep += $keepBak }

        $residue = @($dbFiles | Where-Object { $keep -notcontains $_.Name })
        Write-GuardLog ("DATA 库文件 " + $dbFiles.Count + " 个，应留 " + $keep.Count + " 个（" + ($keep -join "、") + "）")

        if ($residue.Count -gt 0) {
            foreach ($r in $residue) {
                Write-GuardLog ("DATA-RESIDUE " + $r.Name + " " + $r.Length + " 字节")
            }
            if ($AutoCleanData) {
                foreach ($r in $residue) {
                    $safe = ($r.Name -like "verify_*.db") -or ($r.Name -eq "smoke.db") -or ($r.Name -like "b8_*.db") -or ($r.Name -like "mvp.db.bak-*")
                    if (-not $safe) {
                        Write-GuardLog ("DATA-KEEP " + $r.Name + " 不在清理白名单里，留着等人工判断")
                        continue
                    }
                    if ($DryRun) { Write-GuardLog ("[DRYRUN] DATA-DEL " + $r.Name); continue }
                    Remove-Item -LiteralPath $r.FullName -Force -ErrorAction SilentlyContinue
                    if (Test-Path -LiteralPath $r.FullName) {
                        Write-GuardLog ("DATA-DEL-FAIL " + $r.Name)
                    } else {
                        Write-GuardLog ("DATA-DEL " + $r.Name)
                    }
                }
            } else {
                Write-GuardLog ("DATA 发现 " + $residue.Count + " 个残留，未加 -AutoCleanData，只报告")
            }
        } else {
            Write-GuardLog "DATA 干净"
        }
        $state.lastDataCheck = $today
    }
}

# ---------- 3) 落状态 + 日志滚动 ----------
if (-not $DryRun) {
    $json = @{ breach = [int]$state.breach; lastDataCheck = [string]$state.lastDataCheck } | ConvertTo-Json
    [System.IO.File]::WriteAllText($StateFile, $json, (New-Object System.Text.UTF8Encoding($false)))

    if (Test-Path -LiteralPath $LogFile) {
        $lines = @(Get-Content -LiteralPath $LogFile -Encoding UTF8 -ErrorAction SilentlyContinue)
        if ($lines.Count -gt $MaxLogLines) {
            $tail = $lines[($lines.Count - $MaxLogLines)..($lines.Count - 1)]
            [System.IO.File]::WriteAllLines($LogFile, $tail, (New-Object System.Text.UTF8Encoding($false)))
        }
    }
}
