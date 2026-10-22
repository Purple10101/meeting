"""Business logic: coordinates the recorder, the transcriber, the audio files on disk and the database."""
import logging
import subprocess
import threading
import uuid
from datetime import datetime
from pathlib import Path

import soundfile as sf

from db import Database
from recorder import RecorderError
from transcriber import TranscriptionWorker

log = logging.getLogger(__name__)

EXTENSIONS = {"FLAC": ".flac", "WAV": ".wav", "OGG": ".ogg"}


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


class RecordingService:
    def __init__(self, db: Database, recorder, recordings_dir: Path, transcription_engine=None):
        self.db = db
        self.recorder = recorder
        self.recordings_dir = Path(recordings_dir)
        self.recordings_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._current_id: int | None = None
        self._recover_unfinished()

        self.transcriber = None
        if transcription_engine is not None:
            self.transcriber = TranscriptionWorker(db, transcription_engine, self.file_path)
            self.transcriber.start()
            self.transcriber.resume_pending()

    def _recover_unfinished(self) -> None:
        """Rows left as 'recording' mean the app exited mid-recording; keep whatever audio made it to disk."""
        for rec in self.db.unfinished_recordings():
            path = self.file_path(rec)
            duration, size = 0.0, 0
            if path.exists():
                size = path.stat().st_size
                try:
                    duration = sf.info(str(path)).duration
                except Exception:
                    log.warning("Could not read interrupted recording %s", path)
            self.db.finish_recording(rec["id"], "interrupted", duration, size)

    def file_path(self, recording: dict) -> Path:
        return self.recordings_dir / recording["filename"]

    # --- Recording control ------------------------------------------------

    def status(self) -> dict:
        state = self.recorder.state
        return {
            "state": state,
            "recording_id": self._current_id if state != "idle" else None,
            "elapsed_seconds": round(self.recorder.elapsed_seconds(), 2) if state != "idle" else 0,
            "level": round(self.recorder.level(), 3),
        }

    def start(self, title: str | None = None, device: int | None = None) -> dict:
        with self._lock:
            if self.recorder.state != "idle":
                raise RecorderError("A recording is already in progress.")
            now = datetime.now()
            title = (title or "").strip() or f"Meeting {now:%d %b %Y %H:%M}"
            ext = EXTENSIONS.get(self.recorder.file_format, ".audio")
            filename = f"{now:%Y%m%d-%H%M%S}_{uuid.uuid4().hex[:6]}{ext}"

            self.recorder.start(self.recordings_dir / filename, device=device)
            sample_rate, channels = self.recorder.active_format
            self._current_id = self.db.create_recording(
                title, filename, self.recorder.file_format, sample_rate, channels
            )
            return self.db.get_recording(self._current_id)

    def pause(self) -> dict:
        self.recorder.pause()
        return self.status()

    def resume(self) -> dict:
        self.recorder.resume()
        return self.status()

    def stop(self) -> dict:
        with self._lock:
            duration = self.recorder.stop()
            recording_id, self._current_id = self._current_id, None
            rec = self.db.get_recording(recording_id)
            path = self.file_path(rec)
            size = path.stat().st_size if path.exists() else 0
            self.db.finish_recording(recording_id, "completed", duration, size)
            if self.transcriber:
                self.transcriber.submit(recording_id)
            return self.db.get_recording(recording_id)

    def shutdown(self) -> None:
        """Finalise any in-progress recording so the file isn't left half-written, then stop transcribing.

        Unfinished transcriptions stay queued in the database and resume on next launch.
        """
        if self.recorder.state != "idle":
            log.info("Stopping active recording before exit")
            self.stop()
        if self.transcriber:
            self.transcriber.stop()

    # --- Library management -----------------------------------------------

    def list(self, query: str | None = None) -> list[dict]:
        recordings = self.db.list_recordings(query)
        if self.transcriber:
            for rec in recordings:
                if rec["transcript_status"] == "running":
                    rec["transcript_progress"] = self.transcriber.progress(rec["id"])
        return recordings

    def get(self, recording_id: int) -> dict:
        rec = self.db.get_recording(recording_id)
        if rec is None:
            raise NotFound(f"Recording {recording_id} not found.")
        return rec

    def update(self, recording_id: int, title: str | None = None, notes: str | None = None) -> dict:
        self.get(recording_id)
        fields = {}
        if title is not None:
            if not title.strip():
                raise ValueError("Title cannot be empty.")
            fields["title"] = title.strip()
        if notes is not None:
            fields["notes"] = notes
        return self.db.update_recording(recording_id, **fields)

    # --- Transcripts --------------------------------------------------------

    def get_transcript(self, recording_id: int) -> dict:
        self.get(recording_id)
        transcript = self.db.get_transcript(recording_id)
        if transcript is None:
            raise NotFound("This recording has not been transcribed.")
        return transcript

    def transcribe(self, recording_id: int) -> dict:
        """(Re-)queue a recording for transcription, e.g. to retry after a failure."""
        rec = self.get(recording_id)
        if self.transcriber is None:
            raise Conflict("Transcription is not enabled.")
        if rec["status"] == "recording":
            raise Conflict("Stop the recording before transcribing it.")
        if not self.transcriber.submit(recording_id):
            raise Conflict("This recording is already being transcribed.")
        return self.get(recording_id)

    def show_in_folder(self, recording_id: int) -> None:
        path = self.file_path(self.get(recording_id))
        if not path.exists():
            raise NotFound("Audio file is missing.")
        # Opens Explorer with the file highlighted. (Explorer's exit code is meaningless, so don't check it.)
        subprocess.Popen(f'explorer /select,"{path}"')

    def delete(self, recording_id: int) -> None:
        rec = self.get(recording_id)
        if recording_id == self._current_id and self.recorder.state != "idle":
            raise RecorderError("Stop the recording before deleting it.")
        if self.transcriber:
            self.transcriber.cancel(recording_id)
        try:
            self.file_path(rec).unlink(missing_ok=True)
        except PermissionError as exc:
            # Windows refuses to delete files that are open elsewhere (e.g. a media player).
            raise Conflict("The audio file is in use by another program; close it and try again.") from exc
        self.db.delete_recording(recording_id)
