@echo off
setlocal

set "ACTION=%~1"
if "%ACTION%"=="" set "ACTION=status"

set "KEEP_OPEN="
echo %CMDCMDLINE% | findstr /I /C:" /c " >nul && set "KEEP_OPEN=1"

call "%~dp0writer_v1_manual.cmd" %ACTION%
set "EXITCODE=%ERRORLEVEL%"

if defined KEEP_OPEN (
    echo.
    echo [writer_v1_manual] Press any key to close this window...
    pause >nul
)

exit /b %EXITCODE%
