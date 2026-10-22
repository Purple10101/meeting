"""Package the app as a Windows executable: dist\\MeetingRecorder\\MeetingRecorder.exe

Usage:  .venv\\Scripts\\python.exe build.py
"""
from pathlib import Path

import PyInstaller.__main__

from icons import write_ico

ROOT = Path(__file__).resolve().parent
ICON = ROOT / "build" / "MeetingRecorder.ico"

write_ico(ICON)
PyInstaller.__main__.run([
    str(ROOT / "main.py"),
    "--name", "MeetingRecorder",
    "--windowed",  # no console window
    "--onedir",  # starts much faster than --onefile, which unpacks itself on every launch
    "--icon", str(ICON),
    "--add-data", f"{ROOT / 'templates'};templates",
    "--add-data", f"{ROOT / 'static'};static",
    # No PyInstaller hooks exist for these: bundle ctranslate2's DLLs and faster-whisper's VAD model.
    "--collect-all", "ctranslate2",
    "--collect-data", "faster_whisper",
    "--noconfirm",
    "--clean",
])
