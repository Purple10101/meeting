"""Stand-ins for the microphone and the Whisper model, so tests run fast and without hardware."""
import sys
import threading
from pathlib import Path

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from recorder import RecorderError  # noqa: E402
from transcriber import Segment, TranscriptResult  # noqa: E402


class FakeRecorder:
    """Writes one second of silence per 'recording' segment instead of using a microphone."""

    file_format = "FLAC"
    active_format = (16_000, 1)

    def __init__(self):
        self.state = "idle"
        self._path = None
        self._seconds = 0.0

    def start(self, path, device=None):
        if self.state != "idle":
            raise RecorderError("busy")
        self._path, self._seconds, self.state = path, 0.0, "recording"

    def pause(self):
        if self.state != "recording":
            raise RecorderError("not recording")
        self._seconds += 1.0
        self.state = "paused"

    def resume(self):
        if self.state != "paused":
            raise RecorderError("not paused")
        self.state = "recording"

    def stop(self):
        if self.state == "idle":
            raise RecorderError("idle")
        if self.state == "recording":
            self._seconds += 1.0
        sf.write(self._path, np.zeros(int(16_000 * self._seconds), dtype="int16"), 16_000, format="FLAC")
        self.state = "idle"
        return self._seconds

    def elapsed_seconds(self):
        return self._seconds

    def level(self):
        return 0.0


class FakeEngine:
    """Returns a canned transcript. Set `fail` to make it raise, or `gate` to hold it mid-transcription."""

    SEGMENTS = [Segment(0.0, 1.5, "Good morning, team."), Segment(1.5, 3.0, "Action item: ship the prototype.")]

    def __init__(self):
        self.fail = False
        self.gate: threading.Event | None = None
        self.transcribed: list[Path] = []

    def transcribe(self, path, on_progress, should_stop):
        assert Path(path).exists()
        if self.gate:
            on_progress(0.5)
            self.gate.wait(5)
        if self.fail:
            raise RuntimeError("model exploded")
        self.transcribed.append(Path(path))
        return TranscriptResult("fake-model", "en", list(self.SEGMENTS))

    def unload(self):
        pass
