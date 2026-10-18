@echo off
rem Also writes dist\EQOAParser-<version>.zip (exes + config.yaml + READ-ME-FIRST.txt) for sharing.
rem Builds dist\EQOAParser.exe (GUI, asks for Administrator, live by default) and dist\EQOAParser-replay.exe
rem (console, no Administrator, for --pcap / --replay). Needs: pip install -r requirements-dev.txt
cd /d "%~dp0.."
python -m PyInstaller packaging\eqoa.spec --noconfirm --distpath dist --workpath build\pyinstaller
if %errorlevel% neq 0 (
    echo Build failed.
    exit /b 1
)
python packaging\make_zip.py
if %errorlevel% neq 0 (
    echo Zip failed.
    exit /b 1
)
echo.
echo Built:
dir /b dist
