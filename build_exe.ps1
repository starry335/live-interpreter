param(
    [string]$BasePython = $env:LIVE_INTERPRETER_BUILD_PYTHON,
    [string]$BackendPython = $env:LIVE_INTERPRETER_BACKEND_PYTHON
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$venv = Join-Path $root ".packenv311"
$python = Join-Path $venv "Scripts\python.exe"
$pyinstaller = Join-Path $venv "Scripts\pyinstaller.exe"

if (!(Test-Path $python)) {
    if (!$BasePython) {
        $BasePython = "E:\ANACONDA\envs\nemotron_asr\python.exe"
    }
    if (!(Test-Path $BasePython)) {
        throw "Python 3.11 or 3.12 is required. Set LIVE_INTERPRETER_BUILD_PYTHON."
    }
    & $BasePython -c "import audioop, tkinter"
    & $BasePython -m venv $venv
}

& $python -m pip install --disable-pip-version-check pyinstaller -r (Join-Path $root "requirements-cloud.txt")
& $pyinstaller --noconfirm --clean (Join-Path $root "LiveInterpreter.spec")

# The cloud backend must be built with Python 3.13: the 3.10/OpenSSL 1.1
# runtime drops qwen-audio-3.1 WebSocket sessions after a few seconds.
# audioop-lts restores the stdlib module removed in Python 3.13.
$backendVenv = Join-Path $root ".packenv313"
$backendPython = Join-Path $backendVenv "Scripts\python.exe"
$backendPyinstaller = Join-Path $backendVenv "Scripts\pyinstaller.exe"
if (!(Test-Path $backendPython)) {
    if (!$BackendPython) {
        $BackendPython = "python"
    }
    & $BackendPython -c "import sys; assert sys.version_info[:2] >= (3, 13), 'Backend build needs Python 3.13+'"
    & $BackendPython -m venv $backendVenv
}
& $backendPython -m pip install --disable-pip-version-check pyinstaller audioop-lts -r (Join-Path $root "requirements-cloud.txt")
& $backendPyinstaller --noconfirm --clean (Join-Path $root "LiveInterpreterBackend.spec")

Write-Host ""
Write-Host "Built: $(Join-Path $root 'dist\LiveInterpreter')"
Write-Host "Built: $(Join-Path $root 'dist\LiveInterpreterBackend')"
