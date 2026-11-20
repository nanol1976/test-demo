' 无窗口启动 master-worker-platform 主服务（Batch23 收敛为仓库内实装件）
' 路径一律从本脚本位置推出，不再写死机器上的绝对路径，换目录也能直接用。
' 端口固定 8000：与看门狗、看板地址同一口径。
' Run 第三个参数 True = 等子进程结束：计划任务状态才等于服务是否存活；
' 否则 wscript 秒退、任务每分钟被重复触发（多余窗口 + 重复尝试绑 8000 端口）。
Option Explicit
Dim fso, repo, logDir, cmd, sh
Set fso = CreateObject("Scripting.FileSystemObject")
repo = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
logDir = repo & "\run"
If Not fso.FolderExists(logDir) Then fso.CreateFolder logDir
cmd = "cmd.exe /c powershell -NoProfile -ExecutionPolicy Bypass -File """ & repo _
    & "\deploy\run.ps1"" -Port 8000 >> """ & logDir & "\platform.out.log"" 2>&1"
Set sh = CreateObject("WScript.Shell")
sh.Run cmd, 0, True
