[CmdletBinding()]
param(
    [string]$PythonVersion = '3.12',
    [string]$AmazingDataWheel,
    [string]$TgwWheel,
    [string]$SdkPython,
    [string]$EnvFile
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$AppPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'

function Invoke-Python {
    param([string]$Executable, [string[]]$Arguments)
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Python command failed (exit $LASTEXITCODE)." }
}

Push-Location $ProjectRoot
try {
    if (-not (Test-Path -LiteralPath $AppPython)) {
        & py "-$PythonVersion" -m venv (Join-Path $ProjectRoot '.venv')
        if ($LASTEXITCODE -ne 0) { throw "Install Python $PythonVersion (64-bit) with the Windows py launcher first." }
    }
    Invoke-Python $AppPython @('-m', 'pip', 'install', '-r', 'requirements-web.txt', 'pywebview', 'python-dotenv>=1.0,<2')

    if ($SdkPython -and ($AmazingDataWheel -or $TgwWheel)) {
        throw 'Choose an existing -SdkPython OR official wheel files for the project environment.'
    }
    if ($SdkPython) {
        $SdkPython = (Resolve-Path -LiteralPath $SdkPython).Path
        # An external SDK environment is reused read-only; never upgrade its packages here.
    }
    else {
        $SdkPython = $AppPython
        if ($AmazingDataWheel -or $TgwWheel) {
            if (-not $AmazingDataWheel -or -not $TgwWheel) { throw 'Supply both -AmazingDataWheel and -TgwWheel.' }
            $AmazingDataWheel = (Resolve-Path -LiteralPath $AmazingDataWheel).Path
            $TgwWheel = (Resolve-Path -LiteralPath $TgwWheel).Path
            Invoke-Python $AppPython @('-m', 'pip', 'install', $TgwWheel, $AmazingDataWheel)
        }
        Invoke-Python $AppPython @('-m', 'pip', 'install', '-r', 'requirements-galaxy-native.txt')
    }

    $ConfigPath = Join-Path $ProjectRoot '.runtime\galaxy-native.json'
    $Config = [ordered]@{ python = $SdkPython }
    if ($EnvFile) {
        $Config.env_file = (Resolve-Path -LiteralPath $EnvFile).Path
    }
    elseif (Test-Path -LiteralPath $ConfigPath) {
        $Previous = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
        if ($Previous.env_file) { $Config.env_file = $Previous.env_file }
    }
    New-Item -ItemType Directory -Path (Split-Path -Parent $ConfigPath) -Force | Out-Null
    $Config | ConvertTo-Json | Set-Content -LiteralPath $ConfigPath -Encoding UTF8
    Invoke-Python $AppPython @('-c', 'import json,sys; from backend.galaxy_runtime import runtime_status; s=runtime_status(); print(json.dumps(s, ensure_ascii=True)); sys.exit(0 if s.get(''available'') else 1)')
    Write-Host 'Galaxy native runtime is ready locally (no login or download was performed).'
    Write-Host 'Run start.cmd for the desktop app, or start-web.cmd for the local web server.'
}
finally {
    Pop-Location
}
