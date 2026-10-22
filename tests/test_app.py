"""API tests using a fake recorder, so they run without a microphone."""
import numpy as np
import pytest
import soundfile as sf
from fakes import FakeRecorder

from app import create_app
from db import Database
from service import RecordingService


@pytest.fixture
def env(tmp_path):
    db = Database(tmp_path / "test.db")
    service = RecordingService(db, FakeRecorder(), tmp_path / "recordings")
    client = create_app(service).test_client()
    client.environ_base["HTTP_X_REQUESTED_WITH"] = "MeetingRecorder"
    return client, service, tmp_path


def test_full_lifecycle(env):
    client, service, _ = env

    rec = client.post("/api/recording/start", json={"title": "Supervisor sync"}).get_json()
    assert rec["status"] == "recording"
    assert client.get("/api/status").get_json()["state"] == "recording"

    assert client.post("/api/recording/pause").get_json()["state"] == "paused"
    assert client.post("/api/recording/resume").get_json()["state"] == "recording"

    done = client.post("/api/recording/stop").get_json()
    assert done["status"] == "completed"
    assert done["duration_seconds"] == pytest.approx(2.0)
    assert done["size_bytes"] > 0

    audio = client.get(f"/api/recordings/{rec['id']}/audio")
    assert audio.status_code == 200
    audio.close()
    assert sf.info(service.file_path(done)).duration == pytest.approx(2.0)

    updated = client.patch(f"/api/recordings/{rec['id']}", json={"title": "Renamed", "notes": "Action items"}).get_json()
    assert (updated["title"], updated["notes"]) == ("Renamed", "Action items")

    assert client.delete(f"/api/recordings/{rec['id']}").status_code == 204
    assert client.get("/api/recordings").get_json() == []
    assert not service.file_path(done).exists()


def test_default_title_and_single_session(env):
    client, _, _ = env
    rec = client.post("/api/recording/start", json={}).get_json()
    assert rec["title"].startswith("Meeting ")
    assert client.post("/api/recording/start", json={}).status_code == 409
    assert client.delete(f"/api/recordings/{rec['id']}").status_code == 409
    assert client.get(f"/api/recordings/{rec['id']}/audio").status_code == 409


def test_invalid_operations(env):
    client, _, _ = env
    assert client.post("/api/recording/pause").status_code == 409
    assert client.post("/api/recording/stop").status_code == 409
    assert client.get("/api/recordings/999").status_code == 404
    rec = client.post("/api/recording/start", json={"title": "x"}).get_json()
    client.post("/api/recording/stop")
    assert client.patch(f"/api/recordings/{rec['id']}", json={"title": "  "}).status_code == 400


def test_rejects_requests_not_from_our_ui(env):
    _, service, _ = env
    outsider = create_app(service).test_client()  # no X-Requested-With header
    assert outsider.post("/api/recording/start", data={"title": "x"}).status_code == 403
    assert outsider.get("/api/status").status_code == 200
    assert outsider.get("/api/status", headers={"Host": "evil.example:5000"}).status_code == 403
    assert service.recorder.state == "idle"


def test_unfinished_recordings_marked_interrupted_on_startup(tmp_path):
    db = Database(tmp_path / "test.db")
    rec_dir = tmp_path / "recordings"
    rec_dir.mkdir()
    sf.write(rec_dir / "crashed.flac", np.zeros(8_000, dtype="int16"), 16_000, format="FLAC")
    rec_id = db.create_recording("Crashed", "crashed.flac", "FLAC", 16_000, 1)

    RecordingService(db, FakeRecorder(), rec_dir)

    rec = db.get_recording(rec_id)
    assert rec["status"] == "interrupted"
    assert rec["duration_seconds"] == pytest.approx(0.5)
