' Hidden launcher for explorer guard (Batch25)
' Run arg 2 = 0: no console window (otherwise a black window flashes every 2 minutes)
' Run arg 3 = True: wait for it to finish, so the task state means something.
Option Explicit
Dim fso, repo, logDir, cmd, sh
Set fso = CreateObject("Scripting.FileSystemObject")
repo = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
logDir = repo & "\run"
If Not fso.FolderExists(logDir) Then fso.CreateFolder logDir
cmd = "cmd.exe /c powershell -NoProfile -ExecutionPolicy Bypass -File """ & repo _
    & "\deploy\explorer-guard.ps1"" -Restart -AutoCleanData >> """ & logDir & "\explorer-guard.out.log"" 2>&1"
Set sh = CreateObject("WScript.Shell")
sh.Run cmd, 0, True
