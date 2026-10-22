"""Speech-to-text for finished recordings, using faster-whisper on a background worker thread."""
import logging
import os
import queue
import threading
from dataclasses import dataclass
from pathlib import Path

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

log = logging.getLogger(__name__)

# Free the model's (GPU) memory after this long with nothing to transcribe.
IDLE_UNLOAD_SECONDS = 120
# Below this language-detection confidence, the fallback language is used instead of Whisper's guess.
LANGUAGE_CONFIDENCE = 0.5


@dataclass
class Segment:
    start: float
    end: float
    text: str


@dataclass
class TranscriptResult:
    model: str
    language: str
    segments: list[Segment]


class TranscriptionCancelled(Exception):
    pass


class WhisperEngine:
    """Wraps a faster-whisper model, loaded on first use.

    With device="auto" it uses an NVIDIA GPU when CUDA works, and otherwise (or if
    the GPU fails mid-job, e.g. missing CUDA libraries) falls back to the CPU model.
    """

    def __init__(self, gpu_model: str, cpu_model: str, device: str = "auto", language: str | None = None,
                 fallback_language: str = "en", download_root: Path | None = None):
        self.gpu_model = gpu_model
        self.cpu_model = cpu_model
        self.device = device
        self.language = language
        self.fallback_language = fallback_language
        self.download_root = str(download_root) if download_root else None
        self._model = None
        self._model_name = None
        self._loaded_device = None
        self._gpu_failed = False

    def _devices_to_try(self) -> list[str]:
        if self.device != "auto":
            return [self.device]
        import ctranslate2

        use_gpu = not self._gpu_failed and ctranslate2.get_cuda_device_count() > 0
        return ["cuda", "cpu"] if use_gpu else ["cpu"]

    def _load(self, device: str) -> None:
        from faster_whisper import WhisperModel  # heavy import, deferred so the app starts quickly

        name = self.gpu_model if device == "cuda" else self.cpu_model
        log.info("Loading Whisper model %s on %s", name, device)
        self._model = WhisperModel(
            name, device=device, compute_type="float16" if device == "cuda" else "int8",
            download_root=self.download_root,
            # Leave half the cores free so the PC (and any new recording) stays responsive.
            cpu_threads=max(1, (os.cpu_count() or 2) // 2),
        )
        self._model_name, self._loaded_device = name, device

    def unload(self) -> None:
        if self._model is not None:
            log.info("Unloading idle Whisper model")
            self._model = self._model_name = self._loaded_device = None

    def transcribe(self, path: Path, on_progress, should_stop) -> TranscriptResult:
        devices = self._devices_to_try()
        for i, device in enumerate(devices):
            try:
                if self._loaded_device != device:
                    self._load(device)
                return self._run(path, on_progress, should_stop)
            except TranscriptionCancelled:
                raise
            except Exception:
                if i == len(devices) - 1:
                    raise
                log.exception("Transcription on %s failed; falling back to %s", device, devices[i + 1])
                self._gpu_failed = True
                self.unload()

    def _run(self, path: Path, on_progress, should_stop) -> TranscriptResult:
        options = dict(
            beam_size=5,
            vad_filter=True,  # skip silence: faster, and stops Whisper inventing text during quiet stretches
            language_detection_segments=3,  # judge the language from up to 90 s of audio, not just the first 30 s
        )
        # transcribe() detects the language up front but decodes lazily, so re-running here costs little.
        segments, info = self._model.transcribe(str(path), language=self.language, **options)
        if self.language is None and info.language_probability < LANGUAGE_CONFIDENCE:
            # Whisper is unsure what's being spoken (typically short or near-silent audio), and guessing
            # wrong produces confident nonsense in a random language. Use the fallback language instead.
            log.info("Language detection unsure (%s, p=%.2f); using %s",
                     info.language, info.language_probability, self.fallback_language)
            segments, info = self._model.transcribe(str(path), language=self.fallback_language, **options)
        result = []
        for seg in segments:  # decoding happens lazily as we iterate
            if should_stop():
                raise TranscriptionCancelled()
            result.append(Segment(round(seg.start, 2), round(seg.end, 2), seg.text.strip()))
            if info.duration:
                on_progress(min(seg.end / info.duration, 1.0))
        return TranscriptResult(self._model_name, info.language, result)


class TranscriptionWorker:
    """Transcribes recordings one at a time, in the order they were submitted."""

    def __init__(self, db, engine, resolve_path, on_finished=None):
        self.db = db
        self.engine = engine
        self._resolve_path = resolve_path
        self.on_finished = on_finished  # called with (recording, succeeded)
        self._queue: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._pending: set[int] = set()
        self._cancelled: set[int] = set()
        self._progress: dict[int, float] = {}
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="transcriber", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        if self._thread and self._thread.is_alive():
            self._stopping.set()
            self._queue.put(None)
            self._thread.join(timeout)

    def submit(self, recording_id: int) -> bool:
        """Queue a recording. Returns False if it's already queued or being transcribed."""
        with self._lock:
            if recording_id in self._pending:
                return False
            self._pending.add(recording_id)
            self._cancelled.discard(recording_id)
        self.db.queue_transcript(recording_id)
        self._queue.put(recording_id)
        return True

    def resume_pending(self) -> None:
        """Pick up recordings that were never transcribed or were interrupted by the app closing."""
        for recording_id in self.db.recordings_needing_transcription():
            self.submit(recording_id)

    def cancel(self, recording_id: int) -> None:
        with self._lock:
            if recording_id in self._pending:
                self._cancelled.add(recording_id)

    def is_pending(self, recording_id: int) -> bool:
        return recording_id in self._pending

    def progress(self, recording_id: int) -> float | None:
        return self._progress.get(recording_id)

    def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                recording_id = self._queue.get(timeout=IDLE_UNLOAD_SECONDS)
            except queue.Empty:
                self.engine.unload()
                continue
            if recording_id is None:
                break
            try:
                self._process(recording_id)
            finally:
                with self._lock:
                    self._pending.discard(recording_id)
                    self._cancelled.discard(recording_id)
                    self._progress.pop(recording_id, None)

    def _process(self, recording_id: int) -> None:
        rec = self.db.get_recording(recording_id)
        if rec is None or recording_id in self._cancelled:
            return  # deleted while waiting in the queue
        self.db.mark_transcript_running(recording_id)
        self._progress[recording_id] = 0.0
        log.info("Transcribing recording %s", recording_id)
        try:
            result = self.engine.transcribe(
                self._resolve_path(rec),
                on_progress=lambda p: self._progress.__setitem__(recording_id, p),
                should_stop=lambda: self._stopping.is_set() or recording_id in self._cancelled,
            )
        except TranscriptionCancelled:
            log.info("Transcription of recording %s cancelled", recording_id)
            return  # left as 'running', so it's resumed next launch if the app was closing
        except Exception as exc:
            log.exception("Transcription of recording %s failed", recording_id)
            self.db.fail_transcript(recording_id, str(exc) or type(exc).__name__)
            self._notify(rec, False)
            return
        if self.db.complete_transcript(recording_id, result.model, result.language, result.segments):
            log.info("Transcribed recording %s: %d segments (%s)", recording_id, len(result.segments), result.model)
            self._notify(rec, True)

    def _notify(self, rec: dict, succeeded: bool) -> None:
        if self.on_finished:
            try:
                self.on_finished(rec, succeeded)
            except Exception:
                log.exception("on_finished callback failed")
