@echo off
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  set "PYTHON_CMD=python"
) else (
  set "PYTHON_CMD=py -3"
)
if not exist .env (
  %PYTHON_CMD% setup_bot.py
  if errorlevel 1 goto end
)
%PYTHON_CMD% -m projectbot.app
:end
pause
