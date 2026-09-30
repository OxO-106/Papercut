# Start everything Papercut needs: Ollama, the Papercut server, and Tailscale
# Serve (Papercut's private HTTPS address for the laptop), then open Papercut.
# Safe to run again: anything already running is left as it is.
#
#   -App   used by the Start menu entry (launch-papercut.vbs): silent, opens
#          Papercut in its own window, shows a message box only on failure.
#   -NoOpen  used by the tray icon to restart a stopped server: silent like
#          -App, but doesn't open a window.
#   (none) run from a console: shows progress and both addresses.

param([switch]$App, [switch]$NoOpen)
$Silent = $App -or $NoOpen

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$tailscale = "C:\Program Files\Tailscale\tailscale.exe"
$ollama = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe"
$local = "http://localhost:8000"

function Say($text, $color = "Gray") { if (-not $Silent) { Write-Host $text -ForegroundColor $color } }

function Fail($text) {
    if ($Silent) {
        (New-Object -ComObject WScript.Shell).Popup($text, 0, "Papercut", 0x10) | Out-Null
    } else {
        Write-Host $text -ForegroundColor Red
        Read-Host "Press Enter to close"
    }
    exit 1
}

function Warn($text) {
    if ($Silent) { (New-Object -ComObject WScript.Shell).Popup($text, 8, "Papercut", 0x30) | Out-Null }
    else { Write-Host $text -ForegroundColor Yellow }
}

function Test-Url($url) {
    try { Invoke-WebRequest -UseBasicParsing $url -TimeoutSec 5 | Out-Null; return $true } catch { return $false }
}

function Wait-Url($url, $seconds) {
    for ($i = 0; $i -lt $seconds; $i++) {
        if (Test-Url $url) { return $true }
        Start-Sleep 1
    }
    return $false
}

Say "Papercut: starting..." Cyan

# 1. Ollama (the local AI)
if (Test-Url "http://127.0.0.1:11434/api/version") {
    Say "  Ollama         already running"
} elseif (Test-Path $ollama) {
    Start-Process $ollama -ArgumentList "serve" -WindowStyle Hidden
    if (Wait-Url "http://127.0.0.1:11434/api/version" 20) { Say "  Ollama         started" }
    else { Warn "Ollama did not start, so AI features (highlights, Explain, Ask) are unavailable. Reading still works." }
} else {
    Warn "Ollama is not installed, so AI features are unavailable. Reading still works."
}

# 2. Papercut server (background, no window)
if (Test-Url "http://127.0.0.1:8000/api/health") {
    Say "  Papercut       already running"
} else {
    # Hidden, detached from this script, listening on this PC only; output to papercut.log.
    # Keep the previous run's log (it says why that server stopped, if it crashed).
    if (Test-Path "$here\papercut.log") { Move-Item "$here\papercut.log" "$here\papercut.prev.log" -Force -ErrorAction SilentlyContinue }
    $python = "$here\.venv\Scripts\python.exe"
    $cmd = "/c `"`"$python`" -m uvicorn app.main:app --host 127.0.0.1 --port 8000 > `"$here\papercut.log`" 2>&1`""
    Start-Process cmd -ArgumentList $cmd -WorkingDirectory $here -WindowStyle Hidden
    if (Wait-Url "http://127.0.0.1:8000/api/health" 60) { Say "  Papercut       started" }
    else { Fail "The Papercut server did not start. See papercut.log in $here." }
}

# Tray icon that shows whether the server is running (does nothing if already there).
Start-Process powershell -WindowStyle Hidden -ArgumentList "-NoProfile -ExecutionPolicy Bypass -STA -WindowStyle Hidden -File `"$here\papercut-tray.ps1`""

# 3. Tailscale: connected, and serving Papercut on the tailnet for the laptop
$remote = $null
if (-not (Test-Path $tailscale)) {
    Warn "Tailscale is not installed, so the laptop can't reach Papercut. Papercut still works on this PC."
} else {
    $state = (& $tailscale status --json | ConvertFrom-Json)
    if ($state.BackendState -eq "Stopped") {
        & $tailscale up | Out-Null  # reconnect; no sign-in needed
        $state = (& $tailscale status --json | ConvertFrom-Json)
    }
    if ($state.BackendState -ne "Running") {
        # Signed out: signing in happens in the Tailscale app, not here.
        $gui = "C:\Program Files\Tailscale\tailscale-ipn.exe"
        if (Test-Path $gui) { Start-Process $gui }
        Warn "Tailscale needs you to sign in (the Tailscale app is open). Until then, the laptop can't reach Papercut."
    } else {
        Say "  Tailscale      connected as $($state.Self.HostName)"
        # Normally instant. Wait at most 20 s: if Serve were ever disabled on the
        # account, tailscale would wait for approval instead of returning.
        $serve = Start-Process $tailscale -ArgumentList "serve --bg 8000" -WindowStyle Hidden -PassThru
        if ($serve.WaitForExit(20000)) {
            $remote = "https://" + $state.Self.DNSName.TrimEnd(".")
            Say "  Tailscale Serve on"
        } else {
            $serve.Kill()
            Warn "Tailscale Serve didn't start. Run start-papercut.ps1 from a console to see why."
        }
    }
}

# 4. Open Papercut
if ($NoOpen) { exit 0 }
if ($App) {
    # The installed Papercut app if there is one, else an Edge app window.
    . "$here\papercut-open.ps1"
    Open-Papercut
    exit 0
}

Write-Host ""
Write-Host "Papercut is ready." -ForegroundColor Green
Write-Host "  On this PC:     $local"
if ($remote) {
    Write-Host "  On the laptop:  $remote   (copied to clipboard)"
    Set-Clipboard -Value $remote
}
Write-Host ""
Write-Host "Keep this PC on. To stop Papercut, run stop-papercut.bat."
Read-Host "Press Enter to close this window (Papercut keeps running)"
