$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$venv = Join-Path $root ".packenv"
$python = Join-Path $venv "Scripts\python.exe"
$pyinstaller = Join-Path $venv "Scripts\pyinstaller.exe"

if (!(Test-Path $python)) {
    & "E:\ANACONDA\python.exe" -m venv $venv
}

if (!(Test-Path $pyinstaller)) {
    & $python -m pip install pyinstaller
}

& $pyinstaller --noconfirm --clean (Join-Path $root "LiveInterpreter.spec")

Write-Host ""
Write-Host "Built: $(Join-Path $root 'dist\LiveInterpreter_fixed.exe')"
