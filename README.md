# Meeting Recorder

A Windows system-tray app for recording in-person meetings from your computer's microphone.

- **Tray icon:** grey when idle, red while recording, amber when paused. Hover over it to see
  the elapsed time. Right-click to start, pause, resume or stop a recording, open the recordings
  folder, turn on *Start with Windows*, or quit.
- **Window:** left-click the tray icon to open it. From the window you can start, pause and stop
  recordings, and play back, rename, annotate, search, delete or show in Explorer any past
  recording. Closing the window hides it to the tray; any recording carries on.
- **Transcription:** when a recording stops, it's transcribed automatically in the background with
  Whisper, entirely on this PC. The transcript is stored in the database alongside the recording,
  with timestamps; click a timestamp to play from that point. Search covers transcripts too.
- Only one copy runs at a time. Launching it again brings up the existing window.
- Quitting, or Windows shutting down, during a recording saves what was captured.

## Install and run (packaged .exe)

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe build.py
```

This produces `dist\MeetingRecorder\MeetingRecorder.exe`. Copy the whole `dist\MeetingRecorder`
folder wherever you like (e.g. `%LOCALAPPDATA%\Programs\MeetingRecorder`), then pin the exe to
Start or make a desktop shortcut. Use the tray menu's **Start with Windows** to launch it at
login. Autostarted copies start in the tray, without the window.

## Run from source

```powershell
.\.venv\Scripts\pythonw.exe main.py               # no console window
.\.venv\Scripts\python.exe main.py --minimized    # start in the tray only; console shows logs
```

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest tests
```

The tests use a fake recorder, so they don't need a microphone.

## Where things are stored

| What | Where |
|---|---|
| Recordings (audio files) | `Documents\Meeting Recordings` (follows OneDrive folder redirection) |
| Database | `%LOCALAPPDATA%\MeetingRecorder\meetings.db` |
| Log file | `%LOCALAPPDATA%\MeetingRecorder\meeting_recorder.log` |
| Whisper models (downloaded on first use) | `%LOCALAPPDATA%\MeetingRecorder\models` |

You can change these in `config.py`.

## How it fits together

| File | Role |
|---|---|
| `main.py` | Entry point: starts the server, the native window and the tray icon, and handles hide-to-tray and quit |
| `tray.py` | Tray icon, menu and state-coloured icons |
| `app.py` | Flask app that serves the UI and the JSON API to the window, on a random localhost port |
| `service.py` | Business logic: coordinates the recorder, audio files and database |
| `recorder.py` | Microphone capture (`sounddevice`) streamed to disk (`soundfile`), with pause/resume |
| `transcriber.py` | Whisper speech-to-text (`faster-whisper`) and the background worker that runs it |
| `db.py` | SQLite storage for recordings and transcripts |
| `single_instance.py` | Single-instance lock; a second launch signals the first one to show its window |
| `autostart.py` | *Start with Windows* toggle (per-user `Run` registry key; no admin rights needed) |
| `icons.py` | Draws the tray and .exe icons |
| `config.py` | Paths, sample rate, channels and file format |
| `templates/`, `static/` | The UI (plain HTML/CSS/JS), shown in an Edge WebView2 window via `pywebview` |
| `build.py` | PyInstaller packaging |

**Recording pipeline:** PortAudio delivers audio blocks on a real-time thread, and the callback
just queues them. A writer thread encodes them into the file. While paused, blocks are discarded,
so the saved file contains only un-paused audio. If the app is killed mid-recording, the next
startup marks that recording `interrupted` and keeps whatever audio reached disk.

**Transcription pipeline:** stopping a recording queues it with a background worker, which
transcribes one recording at a time. Progress appears in the UI, and a tray notification is shown
when it's done. The database has a `transcripts` table (one row per recording: status, model,
language, full text, any error) and a `transcript_segments` table (timestamped pieces). Both are
deleted along with their recording. Recordings that were never transcribed, or were interrupted
by the app closing, are picked up automatically on the next launch. Failed transcriptions can be
retried from the UI.

**Local API safety:** the API only answers requests addressed to `127.0.0.1` or `localhost`.
Requests that change anything must carry an `X-Requested-With: MeetingRecorder` header.
Together these stop websites open in your browser from controlling the microphone.

## Audio format

The default is **FLAC, 16 kHz, mono, 16-bit**.

- FLAC is lossless (bit-identical to WAV) at about half the size. It needs no external encoder
  such as ffmpeg.
- 16 kHz mono covers the full range of human speech. It is also the input rate of most
  speech-to-text models (e.g. Whisper), so recordings can be transcribed without conversion.
- An hour of recording takes roughly 40–70 MB, depending on how much is silence.

To change the format, edit `config.py`. For example, set `FILE_FORMAT = "WAV"` for maximum
compatibility, or use `SAMPLE_RATE = 48_000` for higher-fidelity audio.

## Transcription settings

Transcription uses [faster-whisper](https://github.com/SYSTRAN/faster-whisper) with OpenAI's
Whisper models. Audio never leaves the PC. The settings are in `config.py`:

| Setting | Default | Notes |
|---|---|---|
| `TRANSCRIBE_DEVICE` | `"auto"` | Uses the NVIDIA GPU if CUDA works, otherwise the CPU |
| `TRANSCRIBE_GPU_MODEL` | `large-v3-turbo` | About 1–2 minutes per hour of audio on an RTX 3060 |
| `TRANSCRIBE_CPU_MODEL` | `small` | About 15 minutes per hour of audio on a Ryzen 7 5800H, using half the cores |
| `TRANSCRIBE_LANGUAGE` | `None` (auto-detect) | Set e.g. `"en"` to skip detection |
| `TRANSCRIBE_FALLBACK_LANGUAGE` | `"en"` | Used when detection is unsure, which stops Whisper inventing text in a random language for quiet recordings |

The GPU path needs the CUDA 12 runtime libraries (cuBLAS, cuDNN 9). This PC has them through the
CUDA Toolkit. On a PC without them, the app falls back to the CPU automatically. The model is
unloaded after 2 minutes idle, so it doesn't hold GPU memory between meetings.
