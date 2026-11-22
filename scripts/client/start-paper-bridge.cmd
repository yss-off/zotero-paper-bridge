@echo off
setlocal
set /p SSH_HOST=Enter SSH Host alias from ~/.ssh/config (e.g. my-gpu-server): 
if "%SSH_HOST%"=="" (
  echo [ERROR] SSH Host is required.
  pause
  exit /b 1
)
powershell -ExecutionPolicy Bypass -File "%~dp0start-paper-bridge.ps1" -SshHost "%SSH_HOST%"
pause
