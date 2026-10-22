"""Meeting Recorder desktop app: a system tray icon plus a native window hosting the UI.

Run from source:  .venv\\Scripts\\pythonw.exe main.py   (add --minimized to start in the tray only)
"""
import argparse
import ctypes
import logging
import os
import sys
import threading
from logging.handlers import RotatingFileHandler

import webview
from werkzeug.serving import make_server

import config
from app import create_app
from db import Database
from recorder import Recorder
from service import RecordingService
from single_instance import SingleInstance
from transcriber import WhisperEngine
from tray import Tray

log = logging.getLogger("meeting_recorder")

SM_SHUTTINGDOWN = 0x2000


def _configure_logging() -> None:
    # A windowed app has no console, so stdout/stderr may be None; libraries that print would crash.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")
    config.APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(config.LOG_PATH, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    logging.basicConfig(
        level=logging.INFO, handlers=[handler, logging.StreamHandler()],
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    for noisy in ("werkzeug", "faster_whisper", "httpx", "huggingface_hub"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main() -> None:
    parser = argparse.ArgumentParser(description=config.APP_NAME)
    parser.add_argument("--minimized", action="store_true", help="start in the system tray without opening the window")
    args = parser.parse_args()

    instance = SingleInstance()
    if instance.already_running:
        instance.signal_existing()
        return

    _configure_logging()
    log.info("Starting %s", config.APP_NAME)

    recorder = Recorder(config.SAMPLE_RATE, config.CHANNELS, config.FILE_FORMAT, config.SUBTYPE)
    engine = None
    if config.TRANSCRIPTION_ENABLED:
        engine = WhisperEngine(
            config.TRANSCRIBE_GPU_MODEL, config.TRANSCRIBE_CPU_MODEL, device=config.TRANSCRIBE_DEVICE,
            language=config.TRANSCRIBE_LANGUAGE, fallback_language=config.TRANSCRIBE_FALLBACK_LANGUAGE,
            download_root=config.MODELS_DIR,
        )
    service = RecordingService(Database(config.DB_PATH), recorder, config.RECORDINGS_DIR, engine)

    # Port 0 lets the OS pick a free port, so we never clash with other local servers.
    server = make_server("127.0.0.1", 0, create_app(service), threaded=True)
    threading.Thread(target=server.serve_forever, name="http", daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/"
    log.info("UI served at %s", url)

    window = webview.create_window(
        config.APP_NAME, url, width=860, height=820, min_size=(420, 520), hidden=args.minimized,
    )
    quitting = threading.Event()
    told_about_tray = threading.Event()

    def show_window():
        window.restore()
        window.show()

    def on_closing():
        if quitting.is_set():
            return True
        if ctypes.windll.user32.GetSystemMetrics(SM_SHUTTINGDOWN):
            # Windows is logging off: hiding would block shutdown, so save and let it close.
            quitting.set()
            service.shutdown()
            return True
        # Closing the window just hides it; the app (and any recording) keeps running in the tray.
        window.hide()
        if not told_about_tray.is_set():
            told_about_tray.set()
            tray.notify("Still running in the system tray. Right-click the icon to quit.")
        return False

    def quit_app():
        quitting.set()
        try:
            if service.recorder.state != "idle":
                rec = service.stop()
                log.info("Saved in-progress recording %s on quit", rec["id"])
        finally:
            tray.stop()
            window.destroy()

    tray = Tray(service, show_window=show_window, quit_app=quit_app)
    window.events.closing += on_closing
    if service.transcriber:
        service.transcriber.on_finished = lambda rec, ok: tray.notify(
            f"Transcript ready: \"{rec['title']}\"" if ok else f"Couldn't transcribe \"{rec['title']}\"; see the app for details."
        )
    instance.on_signal(show_window)

    # Blocks until the window is destroyed (i.e. the user chose Quit).
    webview.start(tray.start)

    service.shutdown()
    server.shutdown()
    log.info("Exited cleanly")


if __name__ == "__main__":
    main()
