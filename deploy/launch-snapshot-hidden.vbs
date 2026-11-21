' ?????Batch30?????? scripts\daily_snapshot.py??? data\snapshots\<??>.html
' ??????????????????? .env ??????????????????
' Run ????? True = ???????????????????
Option Explicit
Dim fso, repo, py, script, q, cmd, sh
Set fso = CreateObject("Scripting.FileSystemObject")
repo = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
py = repo & "\.venv311\Scripts\python.exe"
script = repo & "\scripts\daily_snapshot.py"
If Not fso.FileExists(py) Then
    MsgBox "Missing " & py & vbCrLf & "Run deploy\install-windows-tasks.ps1 first.", 48, "master-worker-platform"
    WScript.Quit 1
End If
If Not fso.FileExists(script) Then
    MsgBox "Missing " & script, 48, "master-worker-platform"
    WScript.Quit 1
End If
q = Chr(34)
cmd = "cmd.exe /c cd /d " & q & repo & q & " && " & q & py & q & " " & q & script & q & " --base-url http://127.0.0.1:8000"
Set sh = CreateObject("WScript.Shell")
sh.Run cmd, 0, True