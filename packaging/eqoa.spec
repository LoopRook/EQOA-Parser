# PyInstaller spec -> dist/EQOAParser.exe (windowed, Administrator) and dist/EQOAParser-replay.exe (console, no UAC).
# Build with packaging\build-exe.bat (or: python -m PyInstaller packaging\eqoa.spec --noconfirm).
# Build-time only; the app itself does not depend on PyInstaller.
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

root = Path(SPECPATH).parent

a = Analysis(
    [str(root / "packaging" / "entry.py")],
    pathex=[str(root)],
    datas=[(str(root / "patterns.yaml"), "."), (str(root / "config.yaml"), ".")]  # bundled defaults
    + [(str(root / "eqoa_parser" / "ui" / "web"), "eqoa_parser/ui/web")]  # web UI: html/css/js + fonts
    + collect_data_files("webview"),  # pywebview js + the WebView2 loader DLLs
    hiddenimports=collect_submodules("eqoa_parser") + collect_submodules("webview"),
    excludes=["pytest", "unittest.mock"],
    noarchive=False,
)
pyz = PYZ(a.pure)

# Live meter: GUI, UAC prompt on launch (raw sockets need Administrator), defaults to --live.
exe_live = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="EQOAParser",
    console=False,
    uac_admin=True,
    upx=False,
)

# Replay/diagnostics: console, runs without Administrator (the uac_admin manifest would prompt even for --pcap).
exe_replay = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="EQOAParser-replay",
    console=True,
    uac_admin=False,
    upx=False,
)
