' Hidden launcher for master-worker-platform (Batch23; Batch27 binds to LAN for phone access)
' Paths are derived from this script's own location, no absolute machine paths.
' Port fixed at 8000: same as watchdog and dashboard addresses.
' Run arg 3 = True: wait for the child, so task state means the service is alive.
Option Explicit
Dim fso, repo, logDir, cmd, sh
Set fso = CreateObject("Scripting.FileSystemObject")
repo = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
logDir = repo & "\run"
If Not fso.FolderExists(logDir) Then fso.CreateFolder logDir
cmd = "cmd.exe /c powershell -NoProfile -ExecutionPolicy Bypass -File """ & repo _
    & "\deploy\run.ps1"" -Port 8000 -Bind 0.0.0.0 >> """ & logDir & "\platform.out.log"" 2>&1"
Set sh = CreateObject("WScript.Shell")
sh.Run cmd, 0, True
