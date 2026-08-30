param([string]$BasePython = $env:LIVE_INTERPRETER_BUILD_PYTHON)

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
& $pyinstaller --noconfirm --clean (Join-Path $root "LiveInterpreterBackend.spec")

Write-Host ""
Write-Host "Built: $(Join-Path $root 'dist\LiveInterpreter')"
Write-Host "Built: $(Join-Path $root 'dist\LiveInterpreterBackend')"
