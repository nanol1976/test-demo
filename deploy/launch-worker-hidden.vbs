' 无窗口启动 worker（Batch23 收敛为仓库内实装件）
' worker 的命令行由 install-windows-tasks.ps1 生成为 run\worker-run.bat：
' 这里不再拼参数，避免"脚本改了、任务里还是旧参数"的两份真相。
' Run 第三个参数 True = 等子进程结束（任务状态=Worker 存活；挂了才能被"失败重启"接住）。
Option Explicit
Dim fso, repo, bat, cmd, sh
Set fso = CreateObject("Scripting.FileSystemObject")
repo = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
bat = repo & "\run\worker-run.bat"
If Not fso.FileExists(bat) Then
    MsgBox "Missing " & bat & vbCrLf & "Run deploy\install-windows-tasks.ps1 first.", 48, "master-worker-platform"
    WScript.Quit 1
End If
cmd = "cmd.exe /c """ & bat & """"
Set sh = CreateObject("WScript.Shell")
sh.Run cmd, 0, True
