@echo off
setlocal
if /i "%~1"=="start" goto start
if /i "%~1"=="help" goto usage
if "%~1"=="" goto usage
>&2 echo Unknown mini command: %~1
goto usage_error

:start
if "%~2"=="" (
    call "%~dp0agent.cmd"
    exit /b %errorlevel%
)
if /i "%~2"=="--desktop" (
    if not "%~3"=="" goto usage_error
    if not exist "%USERPROFILE%\Desktop\" (
        >&2 echo Desktop directory is unavailable: %USERPROFILE%\Desktop
        exit /b 1
    )
    set "AGENT_WORKSPACE=%USERPROFILE%\Desktop"
    call "%~dp0agent.cmd"
    exit /b %errorlevel%
)
if /i "%~2"=="--help" (
    call "%~dp0agent.cmd" --help
    exit /b %errorlevel%
)
if /i "%~2"=="--resume" (
    if "%~3"=="" goto usage_error
    if not "%~4"=="" goto usage_error
    call "%~dp0agent.cmd" --resume "%~3"
    exit /b %errorlevel%
)
goto usage_error

:usage
echo Usage: mini start [--desktop ^| --resume SESSION_ID]
echo        mini help
exit /b 0

:usage_error
>&2 echo Usage: mini start [--desktop ^| --resume SESSION_ID]
>&2 echo        mini help
exit /b 2
