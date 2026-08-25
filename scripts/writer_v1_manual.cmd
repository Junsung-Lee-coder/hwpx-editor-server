@echo off
setlocal

set "ACTION=%~1"
if "%ACTION%"=="" set "ACTION=status"

if /I "%ACTION%"=="start" goto :run
if /I "%ACTION%"=="status" goto :run
if /I "%ACTION%"=="stop" goto :run
if /I "%ACTION%"=="help" goto :help
if /I "%ACTION%"=="/?" goto :help

echo Usage: writer_v1_manual.cmd [start^|status^|stop]
echo.
echo This is the manual on-demand launcher for the writer runtime on this laptop.
echo Run status first for the short operator summary, then use start or stop as needed.
echo Nothing in this wrapper enables auto-start, boot-time launch, or scheduled launch.
exit /b 1

:run
echo [writer_v1_manual] Manual on-demand writer control for this laptop.
if /I "%ACTION%"=="start" echo [writer_v1_manual] Starting the runtime for the current work session only.
if /I "%ACTION%"=="status" echo [writer_v1_manual] Reading the short operator summary, then the packaged launcher snapshot.
if /I "%ACTION%"=="stop" echo [writer_v1_manual] Stopping only the packaged writer processes tracked by writer_v1.
call "%~dp0writer_v1.cmd" %ACTION%
exit /b %errorlevel%

:help
echo Usage: writer_v1_manual.cmd [start^|status^|stop]
echo.
echo Examples:
echo   scripts\writer_v1_manual.cmd start
echo   scripts\writer_v1_manual.cmd status
echo   scripts\writer_v1_manual.cmd stop
echo.
echo This wrapper is intentionally manual. Run it only when you want the writer runtime on this laptop.
echo If you omit the action, it now defaults to status.
exit /b 0
