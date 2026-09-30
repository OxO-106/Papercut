# Open-Papercut: open Papercut as the installed Edge app when there is one
# (sharp title-bar and taskbar icons from the app manifest), otherwise in a
# plain Edge app window, otherwise in the default browser.
# Dot-sourced by start-papercut.ps1 and papercut-tray.ps1.

$script:papercutUrl = "http://localhost:8000/"

# Edge keeps its installed apps per profile in "Sync Data" (LevelDB). Find the
# app whose start address is Papercut's; the id is a 32-letter a-p string.
function Find-PapercutApp {
    $userData = "$env:LOCALAPPDATA\Microsoft\Edge\User Data"
    $pattern = "([a-p]{32})[^a-z]{1,40}" + [regex]::Escape($script:papercutUrl)
    foreach ($profile in Get-ChildItem $userData -Directory -ErrorAction SilentlyContinue) {
        $resources = "$($profile.FullName)\Web Applications\Manifest Resources"
        if (-not (Test-Path $resources)) { continue }
        $files = Get-ChildItem "$($profile.FullName)\Sync Data" -Recurse -File -Include *.ldb, *.log -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending
        foreach ($f in $files) {
            try {
                $bytes = [IO.File]::ReadAllBytes($f.FullName)
            } catch { continue }  # Edge may hold it locked
            $text = [Text.Encoding]::ASCII.GetString($bytes)
            foreach ($m in [regex]::Matches($text, $pattern)) {
                $id = $m.Groups[1].Value
                if (Test-Path "$resources\$id") { return @{ Profile = $profile.Name; Id = $id } }
            }
        }
    }
    return $null
}

function Open-Papercut {
    $edgeDirs = @("${env:ProgramFiles(x86)}\Microsoft\Edge\Application", "$env:ProgramFiles\Microsoft\Edge\Application")
    $proxy = $edgeDirs | ForEach-Object { "$_\msedge_proxy.exe" } | Where-Object { Test-Path $_ } | Select-Object -First 1
    $edge = $edgeDirs | ForEach-Object { "$_\msedge.exe" } | Where-Object { Test-Path $_ } | Select-Object -First 1
    $app = Find-PapercutApp
    if ($app -and $proxy) {
        Start-Process $proxy -ArgumentList "--profile-directory=`"$($app.Profile)`" --app-id=$($app.Id)"
    } elseif ($edge) {
        Start-Process $edge -ArgumentList "--app=$script:papercutUrl"
    } else {
        Start-Process $script:papercutUrl
    }
}
