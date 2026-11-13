# Media bridge for island.py.
# stdout: one JSON line {title, artist, playing, cover(base64), pos, dur (seconds), app (player id)} whenever the media session changes.
# stdin:  commands "play" / "next" / "prev" / "seek <seconds>" sent straight to that session (no fake media keys).
param([int]$ParentPid = 0)
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, $type) {
    $t = $asTask.MakeGenericMethod($type).Invoke($null, @($op))
    $t.Wait(-1) | Out-Null
    $t.Result
}
# type names start with T: PowerShell variables are case-insensitive, so $P would be overwritten by $p
$TMgr = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager, Windows.Media.Control, ContentType = WindowsRuntime]
$TProps = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties, Windows.Media.Control, ContentType = WindowsRuntime]
$TStream = [Windows.Storage.Streams.IRandomAccessStreamWithContentType, Windows.Storage.Streams, ContentType = WindowsRuntime]
Add-Type -Path (Join-Path $PSScriptRoot 'CoverReader.dll')  # built from CoverReader.cs
$mgr = Await ($TMgr::RequestAsync()) $TMgr

function Get-Cover($props) {
    try {
        if (-not $props.Thumbnail) { return '' }
        $ws = Await ($props.Thumbnail.OpenReadAsync()) $TStream
        $b64 = [CoverReader]::Read($ws)
        # плеер может отдать обложку ещё недокачанной (картинка «наполовину»): у целого JPEG в конце метка FF D9
        $b = [Convert]::FromBase64String($b64)
        if ($b.Length -lt 4) { return '' }
        if ($b[0] -eq 0xFF -and $b[1] -eq 0xD8 -and -not ($b[-2] -eq 0xFF -and $b[-1] -eq 0xD9)) { return '' }
        return $b64
    } catch { return '' }
}

# the session that is actually playing wins over Windows' "current" one (e.g. a paused browser tab)
function Get-Session {
    $playing = $mgr.GetSessions() | Where-Object { $_.GetPlaybackInfo().PlaybackStatus -eq 'Playing' } | Select-Object -First 1
    if ($playing) { return $playing }
    return $mgr.GetCurrentSession()
}

# [Console]::In is synchronized, so its ReadLineAsync blocks; a plain StreamReader doesn't
$stdin = New-Object System.IO.StreamReader([Console]::OpenStandardInput())
$pending = $stdin.ReadLineAsync()
$last = ''; $coverKey = ''; $cover = ''; $coverTries = 0; $n = 0
while ($true) {
    if ($ParentPid -and -not (Get-Process -Id $ParentPid -ErrorAction SilentlyContinue)) { exit }

    if ($pending.IsCompleted) {
        $cmd = $pending.Result
        if ($null -eq $cmd) { exit }  # island closed our stdin
        try {
            $s = Get-Session
            if ($s) {
                $c = $cmd.Trim()
                $op = if ($c -eq 'play') { $s.TryTogglePlayPauseAsync() }
                elseif ($c -eq 'next') { $s.TrySkipNextAsync() }
                elseif ($c -eq 'prev') { $s.TrySkipPreviousAsync() }
                elseif ($c -like 'seek *') {
                    $sec = [double]::Parse($c.Substring(5), [Globalization.CultureInfo]::InvariantCulture)
                    $s.TryChangePlaybackPositionAsync([long]($sec * 10000000))  # 100-ns ticks
                }
                if ($op) { Await $op ([bool]) | Out-Null }
            }
        } catch {}
        $pending = $stdin.ReadLineAsync()
        $n = 0  # refresh right away so the button state flips immediately
    }

    if ($n % 4 -eq 0) {
        $info = [ordered]@{ title = ''; artist = ''; playing = $false; cover = ''; pos = 0; dur = 0; app = '' }
        try {
            $s = Get-Session
            if ($s) {
                $p = Await ($s.TryGetMediaPropertiesAsync()) $TProps
                $info.title = [string]$p.Title
                $info.artist = [string]$p.Artist
                $info.app = [string]$s.SourceAppUserModelId
                $info.playing = ($s.GetPlaybackInfo().PlaybackStatus -eq 'Playing')
                $key = "$($info.title)|$($info.artist)"
                if ($key -ne $coverKey) { $coverKey = $key; $cover = ''; $coverTries = 0 }
                # players often publish the picture a moment after the title, so retry a few times
                if (-not $cover -and $coverTries -lt 25) { $coverTries++; $cover = Get-Cover $p }
                $info.cover = $cover
                $tl = $s.GetTimelineProperties()
                $dur = ($tl.EndTime - $tl.StartTime).TotalSeconds
                $pos = $tl.Position.TotalSeconds
                # players report position only now and then; add the time since that report while playing
                $since = ([DateTimeOffset]::Now - $tl.LastUpdatedTime).TotalSeconds
                if ($info.playing -and $since -gt 0 -and $since -lt $dur) { $pos += $since }
                $info.pos = [math]::Round([math]::Max(0, [math]::Min($pos, $dur)), 1)
                $info.dur = [math]::Round([math]::Max(0, $dur), 1)
            }
        } catch {}
        $json = $info | ConvertTo-Json -Compress
        if ($json -ne $last) {
            [Console]::Out.WriteLine($json)
            [Console]::Out.Flush()
            $last = $json
        }
    }
    $n++
    Start-Sleep -Milliseconds 200
}
