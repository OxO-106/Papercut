@echo off
rem Stops the background Papercut server (whatever is listening on port 8000).
rem The marker file tells the tray icon this stop was deliberate (no auto-restart).
type nul > "%~dp0.stop-requested"
powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }; 'Papercut stopped.'"
