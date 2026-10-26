@echo off
setlocal
cd /d "%~dp0"
title Doubao Voice - Keep this window open
if not exist "server.py" goto missing
py -3 --version >nul 2>&1
if not errorlevel 1 goto usepy
python --version >nul 2>&1
if not errorlevel 1 goto usepython
echo Python 3.10 or newer is required. Install Python, then try again.
goto finish
:usepy
py -3 launch.py
goto finish
:usepython
python launch.py
goto finish
:missing
echo server.py was not found. Extract the entire ZIP before starting.
:finish
echo.
echo If startup failed, send a screenshot of this window.
pause
endlocal
