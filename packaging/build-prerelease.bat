@echo off
rem Builds the exes and assembles prerelease\ (git-ignored) for testing with a double-click:
rem EQOAParser.exe, EQOAParser-replay.exe, READ-ME-FIRST.txt and a config.yaml with PINE switched on.
rem Builds into its own staging folder, so a meter already running from dist\ doesn't block the build.
rem Close any copy of the meter running from prerelease\ first.
cd /d "%~dp0.."
python -m PyInstaller packaging\eqoa.spec --noconfirm --distpath build\prerelease-dist --workpath build\pyinstaller-pre
if %errorlevel% neq 0 (
    echo Build failed.
    exit /b 1
)
if exist prerelease rmdir /s /q prerelease
mkdir prerelease
copy /y build\prerelease-dist\EQOAParser.exe prerelease\ >nul
copy /y build\prerelease-dist\EQOAParser-replay.exe prerelease\ >nul
copy /y packaging\READ-ME-FIRST.txt prerelease\ >nul
> prerelease\config.yaml (
    echo # Pre-release settings: PINE is on. Change the slot below to the one shown in PCSX2 ^(Settings ^> Advanced ^> PINE^).
    echo pine: {enabled: true, slot: 28012}
    echo.
    type packaging\config.dist.yaml
)
echo.
echo prerelease\ is ready:
dir /b prerelease
