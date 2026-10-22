"""System tray icon: shows recorder state at a glance and offers quick controls."""
import logging
import os
import threading

import pystray
from pystray import Menu, MenuItem

import autostart
import config
from icons import make_icon

log = logging.getLogger(__name__)

STATE_LABELS = {"idle": "Ready", "recording": "Recording", "paused": "Paused"}


def _format_elapsed(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02}:{s % 3600 // 60:02}:{s % 60:02}"


class Tray:
    def __init__(self, service, show_window, quit_app):
        self.service = service
        self._show_window = show_window
        self._quit_app = quit_app
        self._state = "idle"
        self._icons = {state: make_icon(state) for state in ("idle", "recording", "paused")}
        self._stopped = threading.Event()
        self.icon = pystray.Icon("MeetingRecorder", self._icons["idle"], config.APP_NAME, menu=self._build_menu())

    def _build_menu(self) -> Menu:
        is_state = lambda *states: lambda _item: self._state in states  # noqa: E731
        return Menu(
            MenuItem("Open Meeting Recorder", self._on_open, default=True),
            Menu.SEPARATOR,
            MenuItem("Start recording", self._on_start, visible=is_state("idle")),
            MenuItem("Pause", self._on_pause, visible=is_state("recording")),
            MenuItem("Resume", self._on_resume, visible=is_state("paused")),
            MenuItem("Stop and save", self._on_stop, visible=is_state("recording", "paused")),
            Menu.SEPARATOR,
            MenuItem("Open recordings folder", self._on_open_folder),
            MenuItem("Start with Windows", self._on_toggle_autostart, checked=lambda _: autostart.is_enabled()),
            Menu.SEPARATOR,
            MenuItem("Quit", self._on_quit),
        )

    # --- Lifecycle ----------------------------------------------------------

    def start(self) -> None:
        threading.Thread(target=self.icon.run, name="tray", daemon=True).start()
        threading.Thread(target=self._watch_status, name="tray-status", daemon=True).start()

    def stop(self) -> None:
        self._stopped.set()
        self.icon.stop()

    def notify(self, message: str) -> None:
        try:
            self.icon.notify(message, config.APP_NAME)
        except Exception:
            log.exception("Could not show notification")

    def _watch_status(self) -> None:
        """Mirror the recorder's state, whether it was changed from the tray or from the window."""
        while not self._stopped.wait(0.5):
            status = self.service.status()
            if status["state"] != self._state:
                self._state = status["state"]
                self.icon.icon = self._icons[self._state]
                self.icon.update_menu()
            title = f"{config.APP_NAME} – {STATE_LABELS[self._state]}"
            if self._state != "idle":
                title += f" {_format_elapsed(status['elapsed_seconds'])}"
            if self.icon.title != title:
                self.icon.title = title

    # --- Menu actions (run on the tray thread) ---------------------------------

    def _guarded(self, action, success_message=None) -> None:
        try:
            action()
            if success_message:
                self.notify(success_message)
        except Exception as exc:
            log.warning("Tray action failed: %s", exc)
            self.notify(str(exc))

    def _on_open(self, icon, item):
        self._show_window()

    def _on_start(self, icon, item):
        self._guarded(self.service.start, "Recording started")

    def _on_pause(self, icon, item):
        self._guarded(self.service.pause)

    def _on_resume(self, icon, item):
        self._guarded(self.service.resume)

    def _on_stop(self, icon, item):
        def stop():
            rec = self.service.stop()
            self.notify(f"Saved \"{rec['title']}\" ({_format_elapsed(rec['duration_seconds'])})")

        self._guarded(stop)

    def _on_open_folder(self, icon, item):
        os.startfile(self.service.recordings_dir)

    def _on_toggle_autostart(self, icon, item):
        self._guarded(lambda: autostart.set_enabled(not autostart.is_enabled()))

    def _on_quit(self, icon, item):
        self._quit_app()
