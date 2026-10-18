@echo off
rem EQOA Parser - live DPS meter.
rem Reading the game's network packets needs Administrator, so this asks for it (UAC prompt) and relaunches itself.
rem Extra arguments are passed through, e.g.:  run-live.bat --connection 20605 --show-group

net session >nul 2>&1
if %errorlevel% neq 0 (
    if "%~1"=="" (
        powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    ) else (
        powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '%*' -Verb RunAs"
    )
    exit /b
)

cd /d "%~dp0"
python -m eqoa_parser --live %*
if %errorlevel% neq 0 (
    echo.
    echo The parser exited with an error. See docs\live-capture.md for troubleshooting.
    pause
)
