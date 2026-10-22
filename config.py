"""Application settings. Edit these values to change where data lives or how audio is captured."""
import ctypes
import os
import sys
from pathlib import Path

APP_NAME = "Meeting Recorder"

# When packaged with PyInstaller, bundled files (templates, static) are unpacked to sys._MEIPASS.
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def _documents_dir() -> Path:
    """The user's real Documents folder, which may be redirected (e.g. to OneDrive)."""
    buf = ctypes.create_unicode_buffer(260)
    if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0:  # 5 = CSIDL_PERSONAL
        return Path(buf.value)
    return Path.home() / "Documents"


# App-internal state lives in %LOCALAPPDATA%; the recordings themselves go somewhere the user can find them.
APP_DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "MeetingRecorder"
DB_PATH = APP_DATA_DIR / "meetings.db"
LOG_PATH = APP_DATA_DIR / "meeting_recorder.log"
RECORDINGS_DIR = _documents_dir() / "Meeting Recordings"

# 16 kHz mono is plenty for speech and is the native input rate of most
# speech-to-text models (e.g. Whisper), so recordings can be transcribed as-is.
# If the selected microphone can't capture at this rate, the device's default
# rate is used instead.
SAMPLE_RATE = 16_000
CHANNELS = 1

# FLAC is lossless (identical to WAV) at roughly half the size, plays in all
# modern browsers, and is written natively by libsndfile. Set to "WAV" if you
# need maximum compatibility with older tools.
FILE_FORMAT = "FLAC"
SUBTYPE = "PCM_16"

# Transcription runs locally with faster-whisper (OpenAI Whisper models), so audio never leaves this PC.
# Recordings are transcribed automatically when they're stopped.
TRANSCRIPTION_ENABLED = True
# "auto" uses an NVIDIA GPU when CUDA is usable and falls back to the CPU otherwise; or force "cuda" / "cpu".
TRANSCRIBE_DEVICE = "auto"
# large-v3-turbo is near state-of-the-art and transcribes an hour of audio in ~1-2 minutes on a GPU.
# On CPU it would take ~45 minutes per hour of audio, so a smaller model is used there (~15 minutes).
TRANSCRIBE_GPU_MODEL = "large-v3-turbo"
TRANSCRIBE_CPU_MODEL = "small"
# None auto-detects the spoken language; set e.g. "en" if every meeting is in English.
TRANSCRIBE_LANGUAGE = None
# Used when auto-detection is unsure (usually short or quiet recordings), instead of a likely-wrong guess.
TRANSCRIBE_FALLBACK_LANGUAGE = "en"
# Models are downloaded here on first use (~1.6 GB for large-v3-turbo, ~0.5 GB for small).
MODELS_DIR = APP_DATA_DIR / "models"
