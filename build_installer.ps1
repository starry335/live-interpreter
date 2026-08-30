param([string]$BasePython = $env:LIVE_INTERPRETER_BUILD_PYTHON)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

& (Join-Path $root "build_exe.ps1") -BasePython $BasePython

$iscc = @(
    (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
    "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    "C:\Program Files\Inno Setup 6\ISCC.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1

if (!$iscc) {
    throw "Inno Setup 6 is not installed. Install JRSoftware.InnoSetup with winget."
}

& $iscc (Join-Path $root "installer\LiveInterpreter.iss")
Write-Host "Built: $(Join-Path $root 'dist\installer\LiveInterpreter-Setup-v0.2.1.exe')"
