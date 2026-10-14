@echo off
setlocal
call "%~dp0mini.cmd" start %*
exit /b %errorlevel%
