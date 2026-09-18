[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet('start', 'status', 'stop', 'restart')]
    [string]$Action
)

$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
$RuntimeDir = Join-Path $Root '.runtime'
$PidFile = Join-Path $RuntimeDir 'kline.pid'
$StateFile = Join-Path $RuntimeDir 'kline-state.json'
$PythonExe = Join-Path $Root '.venv\Scripts\python.exe'
$EntryPoint = Join-Path $Root 'webview_app\main_pywebview.py'

function Remove-RuntimeState {
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $StateFile -Force -ErrorAction SilentlyContinue
}

function Get-SavedState {
    if (-not (Test-Path -LiteralPath $StateFile)) {
        return $null
    }

    try {
        return Get-Content -LiteralPath $StateFile -Raw | ConvertFrom-Json
    }
    catch {
        return $null
    }
}

function Get-ManagedProcess {
    if (-not (Test-Path -LiteralPath $PidFile)) {
        return $null
    }

    $savedPid = 0
    if (-not [int]::TryParse((Get-Content -LiteralPath $PidFile -Raw).Trim(), [ref]$savedPid)) {
        return $null
    }

    $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $savedPid" -ErrorAction SilentlyContinue
    if ($null -eq $processInfo) {
        return $null
    }

    $expectedExe = [IO.Path]::GetFullPath($PythonExe)
    $actualExe = if ($processInfo.ExecutablePath) { [IO.Path]::GetFullPath($processInfo.ExecutablePath) } else { '' }
    $expectedEntry = [IO.Path]::GetFullPath($EntryPoint)
    $commandLine = [string]$processInfo.CommandLine

    if (-not $actualExe.Equals($expectedExe, [StringComparison]::OrdinalIgnoreCase)) {
        return $null
    }
    if ($commandLine.IndexOf($expectedEntry, [StringComparison]::OrdinalIgnoreCase) -lt 0) {
        return $null
    }

    return $processInfo
}

function Show-Status {
    $processInfo = Get-ManagedProcess
    if ($null -eq $processInfo) {
        if ((Test-Path -LiteralPath $PidFile) -or (Test-Path -LiteralPath $StateFile)) {
            Remove-RuntimeState
        }
        Write-Host 'KLinePlayground: STOPPED'
        return $false
    }

    $state = Get-SavedState
    $started = if ($state -and $state.startedAt) { [string]$state.startedAt } else { 'unknown' }
    Write-Host "KLinePlayground: RUNNING (PID $($processInfo.ProcessId), started $started)"
    if ($state -and $state.stdoutLog) {
        Write-Host "stdout: $($state.stdoutLog)"
    }
    if ($state -and $state.stderrLog) {
        Write-Host "stderr: $($state.stderrLog)"
        if (Test-Path -LiteralPath $state.stderrLog) {
            $serverLine = Select-String -LiteralPath $state.stderrLog -Pattern 'Running on (http://127\.0\.0\.1:\d+)' -AllMatches -ErrorAction SilentlyContinue | Select-Object -Last 1
            if ($serverLine -and $serverLine.Matches.Count -gt 0) {
                Write-Host "backend: $($serverLine.Matches[0].Groups[1].Value)"
            }
        }
    }
    return $true
}

function Start-App {
    if (Get-ManagedProcess) {
        Show-Status | Out-Null
        return
    }
    Remove-RuntimeState

    if (-not (Test-Path -LiteralPath $PythonExe)) {
        throw "Virtual environment is missing. Expected: $PythonExe"
    }
    if (-not (Test-Path -LiteralPath $EntryPoint)) {
        throw "Application entry point is missing. Expected: $EntryPoint"
    }

    New-Item -ItemType Directory -Path $RuntimeDir -Force | Out-Null
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $stdoutLog = Join-Path $RuntimeDir "kline-$stamp.out.log"
    $stderrLog = Join-Path $RuntimeDir "kline-$stamp.err.log"

    $oldUnbuffered = $env:PYTHONUNBUFFERED
    $oldEncoding = $env:PYTHONIOENCODING
    $env:PYTHONUNBUFFERED = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    try {
        $process = Start-Process -FilePath $PythonExe `
            -ArgumentList @('-u', ('"{0}"' -f $EntryPoint)) `
            -WorkingDirectory $Root `
            -WindowStyle Hidden `
            -RedirectStandardOutput $stdoutLog `
            -RedirectStandardError $stderrLog `
            -PassThru
    }
    finally {
        $env:PYTHONUNBUFFERED = $oldUnbuffered
        $env:PYTHONIOENCODING = $oldEncoding
    }

    Set-Content -LiteralPath $PidFile -Value $process.Id -Encoding ASCII
    [ordered]@{
        pid = $process.Id
        startedAt = (Get-Date).ToString('o')
        executable = $PythonExe
        entryPoint = $EntryPoint
        stdoutLog = $stdoutLog
        stderrLog = $stderrLog
    } | ConvertTo-Json | Set-Content -LiteralPath $StateFile -Encoding UTF8

    $deadline = (Get-Date).AddSeconds(20)
    do {
        Start-Sleep -Milliseconds 500
        $process.Refresh()
        if ($process.HasExited) {
            $exitCode = $process.ExitCode
            Remove-RuntimeState
            Write-Host "KLinePlayground failed to start (exit code $exitCode)." -ForegroundColor Red
            if (Test-Path -LiteralPath $stderrLog) {
                Get-Content -LiteralPath $stderrLog -Encoding UTF8 -Tail 30
            }
            throw 'Application startup failed.'
        }

        if ((Test-Path -LiteralPath $stderrLog) -and
            (Select-String -LiteralPath $stderrLog -SimpleMatch 'Running on http://' -Quiet -ErrorAction SilentlyContinue)) {
            Start-Sleep -Seconds 1
            break
        }
    } while ((Get-Date) -lt $deadline)

    Write-Host "KLinePlayground started (PID $($process.Id))."
    Write-Host 'The desktop window should now be visible; the console process stays hidden.'
    Write-Host "stdout: $stdoutLog"
    Write-Host "stderr: $stderrLog"
}

function Stop-App {
    $processInfo = Get-ManagedProcess
    if ($null -eq $processInfo) {
        Remove-RuntimeState
        Write-Host 'KLinePlayground is already stopped.'
        return
    }

    $processId = [int]$processInfo.ProcessId
    # Keep the validated root alive until taskkill enumerates its tree. Closing the
    # GUI first can orphan an active SDK worker, since Windows venv adds a redirector.
    $killer = Start-Process -FilePath "$env:SystemRoot\System32\taskkill.exe" `
        -ArgumentList @('/PID', [string]$processId, '/T', '/F') `
        -WindowStyle Hidden -Wait -PassThru
    Wait-Process -Id $processId -Timeout 5 -ErrorAction SilentlyContinue
    if (Get-ManagedProcess) { throw 'Application process tree did not stop; runtime state retained.' }

    Remove-RuntimeState
    Write-Host "KLinePlayground stopped (PID $processId)."
}

switch ($Action) {
    'start' {
        Start-App
    }
    'status' {
        if (-not (Show-Status)) {
            exit 3
        }
    }
    'stop' {
        Stop-App
    }
    'restart' {
        Stop-App
        Start-App
    }
}
