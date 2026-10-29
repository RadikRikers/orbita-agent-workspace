@echo off
cd /d "%~dp0"
title Orbita - Agent Workspace
where py >nul 2>&1
if %errorlevel%==0 (
  py -3 server.py
) else (
  python server.py
)
if errorlevel 1 (
  echo.
  echo Install Python 3.10+ from https://www.python.org/downloads/
  echo Enable "Add Python to PATH" during installation.
)
pause
