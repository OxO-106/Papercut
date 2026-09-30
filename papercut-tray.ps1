# Papercut icon in the taskbar's notification area (system tray).
# Shows whether the Papercut server is running (checked every 5 seconds):
#   coloured icon = running, grey icon = stopped.
# Double-click opens Papercut; right-click for more. Started by
# start-papercut.ps1; only one copy runs at a time.

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Windows.Forms, System.Drawing

$created = $false
$mutex = New-Object System.Threading.Mutex($true, "Local\PapercutTray", [ref]$created)
if (-not $created) { exit 0 }  # already in the tray

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$health = "http://127.0.0.1:8000/api/health"

# Icons: the app icon, and a greyed copy for "stopped".
$size = [System.Windows.Forms.SystemInformation]::SmallIconSize
$onIcon = New-Object System.Drawing.Icon("$here\web\icons\papercut.ico", $size)
$bmp = $onIcon.ToBitmap()
$grey = New-Object System.Drawing.Bitmap($bmp.Width, $bmp.Height)
$g = [System.Drawing.Graphics]::FromImage($grey)
[System.Windows.Forms.ControlPaint]::DrawImageDisabled($g, $bmp, 0, 0, [System.Drawing.Color]::Transparent)
$g.Dispose()
$offIcon = [System.Drawing.Icon]::FromHandle($grey.GetHicon())

# The laptop's address (Tailscale Serve), if Tailscale is connected.
$remote = $null
try {
    $ts = & "C:\Program Files\Tailscale\tailscale.exe" status --json | ConvertFrom-Json
    if ($ts.BackendState -eq "Running") { $remote = "https://" + $ts.Self.DNSName.TrimEnd(".") }
} catch {}

function Test-Server {
    try {
        $req = [System.Net.WebRequest]::Create($health)
        $req.Timeout = 1500
        $res = $req.GetResponse()
        $res.Close()
        return $true
    } catch { return $false }
}

. "$here\papercut-open.ps1"  # Open-Papercut: the installed app, else an Edge app window

$tray = New-Object System.Windows.Forms.NotifyIcon
$menu = New-Object System.Windows.Forms.ContextMenuStrip
$status = $menu.Items.Add("Papercut")
$status.Enabled = $false
[void]$menu.Items.Add("-")
$open = $menu.Items.Add("Open Papercut")
$open.Font = New-Object System.Drawing.Font($open.Font, [System.Drawing.FontStyle]::Bold)
$open.add_Click({ Open-Papercut })
if ($remote) {
    $copy = $menu.Items.Add("Copy laptop link")
    $copy.ToolTipText = $remote
    $copy.add_Click({ [System.Windows.Forms.Clipboard]::SetText($remote) })
}
[void]$menu.Items.Add("-")
$start = $menu.Items.Add("Start Papercut")
$start.add_Click({ Start-PapercutServer -Open })
$stop = $menu.Items.Add("Stop Papercut")
$stop.add_Click({
    New-Item -ItemType File -Force $stopMarker | Out-Null  # a deliberate stop: don't restart
    Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue |
        ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
    Update-Status
})
[void]$menu.Items.Add("-")
$quit = $menu.Items.Add("Hide this icon")
$quit.add_Click({ $tray.Visible = $false; [System.Windows.Forms.Application]::Exit() })
$tray.ContextMenuStrip = $menu
$tray.add_DoubleClick({ Open-Papercut })

# stop-papercut.bat and "Stop Papercut" leave this file, so a deliberate stop
# isn't mistaken for a crash. It is cleared once a server started after it is
# running (however it was started), which re-arms the auto-restart.
$stopMarker = "$here\.stop-requested"
$eventLog = "$here\papercut-events.log"
function Write-Event($text) {
    Add-Content -Path $eventLog -Value ("{0:yyyy-MM-dd HH:mm:ss}  {1}" -f (Get-Date), $text) -ErrorAction SilentlyContinue
}

function Start-PapercutServer([switch]$Open) {
    Remove-Item $stopMarker -ErrorAction SilentlyContinue
    $mode = if ($Open) { "-App" } else { "-NoOpen" }
    Start-Process powershell -WindowStyle Hidden -ArgumentList "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$here\start-papercut.ps1`" $mode"
}

$script:running = $null
$script:restarts = @()  # times of automatic restarts, to give up if it keeps failing
function Clear-StaleStopMarker {
    if (-not (Test-Path $stopMarker)) { return }
    $conn = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    $proc = if ($conn) { Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue }
    if ($proc -and $proc.StartTime -gt (Get-Item $stopMarker).LastWriteTime) { Remove-Item $stopMarker -ErrorAction SilentlyContinue }
}

function Update-Status {
    $up = Test-Server
    if ($up) { Clear-StaleStopMarker }
    if ($up -eq $script:running) { return }
    $was = $script:running
    $script:running = $up
    $tray.Icon = if ($up) { $onIcon } else { $offIcon }
    $tray.Text = if ($up) { "Papercut: running" } else { "Papercut: stopped" }  # tooltip (max 63 chars)
    $status.Text = if ($up) { "Papercut is running" } else { "Papercut is stopped" }
    $open.Enabled = $up
    $start.Visible = -not $up
    $stop.Visible = $up
    if ($up) {
        if ($was -eq $false) { Write-Event "server running" }
        return
    }
    if ($was -ne $true) { return }
    if (Test-Path $stopMarker) { Write-Event "server stopped (on request)"; return }
    # Stopped without being asked to: note it, and start it again (at most 3 times an hour).
    $script:restarts = @($script:restarts | Where-Object { $_ -gt (Get-Date).AddHours(-1) })
    if ($script:restarts.Count -lt 3) {
        $script:restarts += Get-Date
        Write-Event "server stopped unexpectedly; restarting (see papercut.prev.log)"
        Start-PapercutServer
        $tray.ShowBalloonTip(5000, "Papercut restarting", "The Papercut server stopped unexpectedly and is being started again.", "Warning")
    } else {
        Write-Event "server stopped unexpectedly; not restarting (3 restarts in the last hour)"
        $tray.ShowBalloonTip(8000, "Papercut stopped", "The Papercut server keeps stopping. See papercut.prev.log in $here. Right-click the icon to start it again.", "Error")
    }
}

$tray.Visible = $true
Write-Event "tray icon started"
Update-Status
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 5000
$timer.add_Tick({ Update-Status })
$timer.Start()

[System.Windows.Forms.Application]::Run()
$timer.Stop()
$tray.Dispose()
$mutex.ReleaseMutex()
