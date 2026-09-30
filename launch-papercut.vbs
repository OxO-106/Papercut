' Start-menu launcher: starts Ollama, the Papercut server and Tailscale Serve,
' then opens Papercut in its own window. Runs start-papercut.ps1 -App with no
' console window; problems are shown in a message box.
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
CreateObject("WScript.Shell").Run "powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & here & "\start-papercut.ps1"" -App", 0, False
