"""Transcription pipeline tests, using a fake Whisper engine."""
import threading
import time

import numpy as np
import pytest
import soundfile as sf
from fakes import FakeEngine, FakeRecorder

from app import create_app
from db import Database
from service import RecordingService


def wait_for(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.02)
    raise AssertionError("Timed out waiting for condition")


@pytest.fixture
def env(tmp_path):
    engine = FakeEngine()
    service = RecordingService(Database(tmp_path / "test.db"), FakeRecorder(), tmp_path / "recordings", engine)
    client = create_app(service).test_client()
    client.environ_base["HTTP_X_REQUESTED_WITH"] = "MeetingRecorder"
    yield client, service, engine
    service.shutdown()


def record(client, title="Weekly sync"):
    rec = client.post("/api/recording/start", json={"title": title}).get_json()
    client.post("/api/recording/stop")
    return rec["id"]


def transcript_status(client, recording_id):
    return client.get(f"/api/recordings/{recording_id}").get_json()["transcript_status"]


def test_transcribed_automatically_after_stop(env):
    client, _, _ = env
    rec_id = record(client)
    wait_for(lambda: transcript_status(client, rec_id) == "completed")

    t = client.get(f"/api/recordings/{rec_id}/transcript").get_json()
    assert t["text"] == "Good morning, team. Action item: ship the prototype."
    assert (t["model"], t["language"]) == ("fake-model", "en")
    assert t["segments"] == [
        {"start": 0.0, "end": 1.5, "text": "Good morning, team."},
        {"start": 1.5, "end": 3.0, "text": "Action item: ship the prototype."},
    ]


def test_not_transcribed_while_recording(env):
    client, _, engine = env
    rec = client.post("/api/recording/start", json={}).get_json()
    assert rec["transcript_status"] is None
    assert client.post(f"/api/recordings/{rec['id']}/transcribe").status_code == 409
    assert client.get(f"/api/recordings/{rec['id']}/transcript").status_code == 404
    assert engine.transcribed == []


def test_search_covers_transcripts(env):
    client, _, _ = env
    rec_id = record(client, "Budget review")
    wait_for(lambda: transcript_status(client, rec_id) == "completed")

    def search(q):
        return [r["id"] for r in client.get("/api/recordings", query_string={"q": q}).get_json()]

    assert search("prototype") == [rec_id]  # transcript
    assert search("budget") == [rec_id]  # title
    assert search("nonexistent") == []
    assert search("100%") == []  # LIKE wildcards are matched literally


def test_progress_reported_while_running(env):
    client, _, engine = env
    engine.gate = threading.Event()
    rec_id = record(client)
    wait_for(lambda: transcript_status(client, rec_id) == "running")
    listed = client.get("/api/recordings").get_json()[0]
    assert listed["transcript_progress"] == 0.5
    assert client.post(f"/api/recordings/{rec_id}/transcribe").status_code == 409  # already in progress
    engine.gate.set()
    wait_for(lambda: transcript_status(client, rec_id) == "completed")


def test_failure_is_recorded_and_can_be_retried(env):
    client, _, engine = env
    engine.fail = True
    rec_id = record(client)
    wait_for(lambda: transcript_status(client, rec_id) == "failed")
    assert client.get(f"/api/recordings/{rec_id}").get_json()["transcript_error"] == "model exploded"

    engine.fail = False
    assert client.post(f"/api/recordings/{rec_id}/transcribe").status_code == 202
    wait_for(lambda: transcript_status(client, rec_id) == "completed")
    assert client.get(f"/api/recordings/{rec_id}").get_json()["transcript_error"] is None


def test_delete_removes_transcript(env):
    client, service, _ = env
    rec_id = record(client)
    wait_for(lambda: transcript_status(client, rec_id) == "completed")
    assert client.delete(f"/api/recordings/{rec_id}").status_code == 204
    assert service.db.get_transcript(rec_id) is None
    with service.db._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM transcript_segments").fetchone()[0] == 0


def test_delete_during_transcription_discards_result(env):
    client, service, engine = env
    engine.gate = threading.Event()
    rec_id = record(client)
    wait_for(lambda: transcript_status(client, rec_id) == "running")
    assert client.delete(f"/api/recordings/{rec_id}").status_code == 204
    engine.gate.set()
    wait_for(lambda: not service.transcriber.is_pending(rec_id))
    assert service.db.get_transcript(rec_id) is None


def test_backlog_transcribed_on_startup(tmp_path):
    """Recordings made before transcription existed, or interrupted mid-transcription, get picked up."""
    db = Database(tmp_path / "test.db")
    rec_dir = tmp_path / "recordings"
    rec_dir.mkdir()
    ids = []
    for name in ("old.flac", "interrupted.flac"):
        sf.write(rec_dir / name, np.zeros(16_000, dtype="int16"), 16_000, format="FLAC")
        rec_id = db.create_recording(name, name, "FLAC", 16_000, 1)
        db.finish_recording(rec_id, "completed", 1.0, 100)
        ids.append(rec_id)
    db.queue_transcript(ids[1])
    db.mark_transcript_running(ids[1])  # the app closed while this one was being transcribed

    service = RecordingService(db, FakeRecorder(), rec_dir, FakeEngine())
    try:
        wait_for(lambda: all(db.get_transcript(i)["status"] == "completed" for i in ids if db.get_transcript(i)))
        assert all(db.get_transcript(i)["status"] == "completed" for i in ids)
    finally:
        service.shutdown()
