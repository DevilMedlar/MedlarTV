@echo off
setlocal DisableDelayedExpansion
set "PRIVATE_REPO=%~dp0"
set "PY=%PRIVATE_REPO%.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [ERROR] Project virtual-environment Python is missing. No build or Git action ran.
  exit /b 2
)
"%PY%" -B "%PRIVATE_REPO%MedlarTV\tools\update_commands_site.py" %*
exit /b %errorlevel%
