@echo off
setlocal
set "PY=E:\ANACONDA\envs\funasr_py38\python.exe"
set "NO_PROXY=*"
set "no_proxy=*"
set "MODELSCOPE_CACHE=E:\FunAsr\models"
set "HF_HOME=E:\FunAsr\models\huggingface"
set "TRANSFORMERS_CACHE=E:\FunAsr\models\huggingface"

if not exist "%PY%" (
  echo Python environment not found: %PY%
  exit /b 1
)

"%PY%" "%~dp0live_interpreter.py" %*
