' 无窗口启动看门狗（Batch23 收敛为仓库内实装件）
' Run 第二参数 0 = 不创建控制台；第三个 True = 等它跑完，任务状态才有意义。
Option Explicit
Dim fso, repo, logDir, cmd, sh
Set fso = CreateObject("Scripting.FileSystemObject")
repo = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
logDir = repo & "\run"
If Not fso.FolderExists(logDir) Then fso.CreateFolder logDir
cmd = "cmd.exe /c powershell -NoProfile -ExecutionPolicy Bypass -File """ & repo _
    & "\deploy\worker-windows-watchdog.ps1"" >> """ & logDir & "\watchdog.out.log"" 2>&1"
Set sh = CreateObject("WScript.Shell")
sh.Run cmd, 0, True
