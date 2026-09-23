@echo off
setlocal
set "AGENT_ROOT=%~dp0"
if not exist "%AGENT_ROOT%main.py" (
    >&2 echo mini-agent startup failed: main.py is missing beside agent.cmd.
    exit /b 1
)
if not exist "%AGENT_ROOT%.venv\Scripts\python.exe" (
    >&2 echo mini-agent startup failed: create the project .venv and install requirements.txt first.
    exit /b 1
)
"%AGENT_ROOT%.venv\Scripts\python.exe" "%AGENT_ROOT%main.py" %*
exit /b %errorlevel%
