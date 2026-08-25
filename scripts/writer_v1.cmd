@echo off
setlocal
powershell -ExecutionPolicy Bypass -File "%~dp0writer_v1.ps1" %*
exit /b %errorlevel%
