"""Microphone capture with pause/resume, streamed straight to an audio file on disk."""
import logging
import queue
import threading
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf

log = logging.getLogger(__name__)


class RecorderError(Exception):
    pass


def list_input_devices() -> list[dict]:
    """Input devices for the default host API (avoids listing every mic once per Windows audio driver)."""
    try:
        default_input = sd.default.device[0]
        hostapi = sd.query_devices(default_input)["hostapi"] if default_input is not None and default_input >= 0 else 0
        return [
            {"index": d["index"], "name": d["name"], "is_default": d["index"] == default_input}
            for d in sd.query_devices()
            if d["max_input_channels"] > 0 and d["hostapi"] == hostapi
        ]
    except Exception:
        log.exception("Could not query audio devices")
        return []


class Recorder:
    """Records one session at a time.

    The PortAudio callback runs on a real-time thread, so it only copies samples
    into a queue; a separate writer thread does the (slower) file encoding.
    While paused, incoming audio is discarded, so the saved file contains only
    the un-paused portions and its length equals the reported elapsed time.
    """

    def __init__(self, sample_rate: int, channels: int, file_format: str, subtype: str):
        self.sample_rate = sample_rate
        self.channels = channels
        self.file_format = file_format
        self.subtype = subtype

        self._lock = threading.Lock()
        self._state = "idle"  # idle | recording | paused
        self._stream = None
        self._file = None
        self._queue: queue.Queue | None = None
        self._writer: threading.Thread | None = None
        self._active_rate = sample_rate
        self._active_channels = channels
        self._frames_written = 0
        self._level = 0.0

    @property
    def state(self) -> str:
        return self._state

    @property
    def active_format(self) -> tuple[int, int]:
        """(sample_rate, channels) actually in use for the current recording."""
        return self._active_rate, self._active_channels

    def elapsed_seconds(self) -> float:
        return self._frames_written / self._active_rate if self._active_rate else 0.0

    def level(self) -> float:
        """Peak input level of the latest audio block, 0.0-1.0."""
        return self._level if self._state == "recording" else 0.0

    def _negotiate_format(self, device) -> tuple[int, int]:
        info = sd.query_devices(device, "input")
        channels = min(self.channels, int(info["max_input_channels"])) or 1
        try:
            sd.check_input_settings(device=device, samplerate=self.sample_rate, channels=channels, dtype="int16")
            return self.sample_rate, channels
        except Exception:
            fallback = int(info["default_samplerate"])
            log.warning("Device does not support %s Hz; falling back to %s Hz", self.sample_rate, fallback)
            return fallback, channels

    def start(self, path: Path, device: int | None = None) -> None:
        with self._lock:
            if self._state != "idle":
                raise RecorderError("A recording is already in progress.")
            try:
                self._active_rate, self._active_channels = self._negotiate_format(device)
            except Exception as exc:
                raise RecorderError(f"No usable microphone found: {exc}") from exc

            self._file = sf.SoundFile(
                path, mode="w", samplerate=self._active_rate, channels=self._active_channels,
                format=self.file_format, subtype=self.subtype,
            )
            self._queue = queue.Queue()
            self._frames_written = 0
            self._level = 0.0
            try:
                self._stream = sd.InputStream(
                    samplerate=self._active_rate, channels=self._active_channels, dtype="int16",
                    device=device, callback=self._on_audio,
                )
                self._writer = threading.Thread(target=self._write_loop, name="recorder-writer", daemon=True)
                self._writer.start()
                self._state = "recording"
                self._stream.start()
            except Exception as exc:
                self._teardown()
                Path(path).unlink(missing_ok=True)
                raise RecorderError(f"Could not open microphone: {exc}") from exc

    def pause(self) -> None:
        with self._lock:
            if self._state != "recording":
                raise RecorderError("Nothing is recording.")
            self._state = "paused"

    def resume(self) -> None:
        with self._lock:
            if self._state != "paused":
                raise RecorderError("Recording is not paused.")
            self._state = "recording"

    def stop(self) -> float:
        """Stop and finalise the file. Returns the recorded duration in seconds."""
        with self._lock:
            if self._state == "idle":
                raise RecorderError("Nothing is recording.")
            self._teardown()
            return self.elapsed_seconds()

    def _teardown(self) -> None:
        self._state = "idle"
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            finally:
                self._stream = None
        if self._writer is not None:
            self._queue.put(None)
            self._writer.join()
            self._writer = None
        if self._file is not None:
            self._file.close()
            self._file = None

    def _on_audio(self, indata: np.ndarray, frames, time_info, status) -> None:
        if status:
            log.debug("Audio stream status: %s", status)
        if self._state != "recording":
            return
        self._level = float(np.abs(indata).max()) / 32768.0
        self._queue.put(indata.copy())

    def _write_loop(self) -> None:
        while (block := self._queue.get()) is not None:
            self._file.write(block)
            self._frames_written += len(block)
