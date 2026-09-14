@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_windows.ps1" -SourceRoot "%~dp0.." -InstallRoot "%~dp0..\.hwpx-install" -DependencyMode InstallUserScope
set "EXIT_CODE=%ERRORLEVEL%"
endlocal & exit /b %EXIT_CODE%
