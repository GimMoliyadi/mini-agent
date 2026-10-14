@echo off
setlocal
set "AGENT_ROOT=%~dp0"
if not exist "%AGENT_ROOT%launcher.py" goto missing_launcher
if exist "%AGENT_ROOT%.venv\Scripts\python.exe" goto virtualenv
py -3.11 "%AGENT_ROOT%launcher.py" %*
exit /b %errorlevel%

:virtualenv
"%AGENT_ROOT%.venv\Scripts\python.exe" "%AGENT_ROOT%launcher.py" %*
exit /b %errorlevel%

:missing_launcher
>&2 echo mini-agent startup failed: launcher.py is missing beside mini.cmd.
exit /b 1
