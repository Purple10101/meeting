"""Flask app: serves the UI and a small JSON API over the recording service. Launched by main.py."""
from flask import Flask, abort, jsonify, render_template, request, send_file

import config
from recorder import RecorderError, list_input_devices
from service import Conflict, NotFound, RecordingService

LOOPBACK_HOSTS = {"127.0.0.1", "localhost"}
CLIENT_HEADER = ("X-Requested-With", "MeetingRecorder")


def create_app(service: RecordingService) -> Flask:
    app = Flask(
        __name__,
        template_folder=str(config.BUNDLE_DIR / "templates"),
        static_folder=str(config.BUNDLE_DIR / "static"),
    )

    @app.before_request
    def only_our_ui():
        # Reject requests addressed to any other host name (defeats DNS-rebinding attacks).
        if request.host.rsplit(":", 1)[0] not in LOOPBACK_HOSTS:
            abort(403)
        # Websites can't add custom headers to cross-site requests without a CORS preflight,
        # which we never grant, so this stops other pages from controlling the microphone.
        if request.method not in ("GET", "HEAD") and request.headers.get(CLIENT_HEADER[0]) != CLIENT_HEADER[1]:
            abort(403)

    @app.errorhandler(RecorderError)
    @app.errorhandler(Conflict)
    def conflict(exc):
        return jsonify(error=str(exc)), 409

    @app.errorhandler(NotFound)
    def not_found(exc):
        return jsonify(error=str(exc)), 404

    @app.errorhandler(ValueError)
    def bad_request(exc):
        return jsonify(error=str(exc)), 400

    @app.get("/")
    def index():
        return render_template("index.html")

    # --- Recording control ---

    @app.get("/api/status")
    def status():
        return jsonify(service.status())

    @app.get("/api/devices")
    def devices():
        return jsonify(list_input_devices())

    @app.post("/api/recording/start")
    def start():
        body = request.get_json(silent=True) or {}
        device = body.get("device")
        rec = service.start(title=body.get("title"), device=int(device) if device not in (None, "") else None)
        return jsonify(rec), 201

    @app.post("/api/recording/pause")
    def pause():
        return jsonify(service.pause())

    @app.post("/api/recording/resume")
    def resume():
        return jsonify(service.resume())

    @app.post("/api/recording/stop")
    def stop():
        return jsonify(service.stop())

    # --- Library ---

    @app.get("/api/recordings")
    def list_recordings():
        """Optional ?q= searches titles, notes and transcripts."""
        return jsonify(service.list(request.args.get("q", "").strip() or None))

    @app.get("/api/recordings/<int:recording_id>")
    def get_recording(recording_id):
        return jsonify(service.get(recording_id))

    @app.patch("/api/recordings/<int:recording_id>")
    def update_recording(recording_id):
        body = request.get_json(silent=True) or {}
        return jsonify(service.update(recording_id, title=body.get("title"), notes=body.get("notes")))

    @app.delete("/api/recordings/<int:recording_id>")
    def delete_recording(recording_id):
        service.delete(recording_id)
        return "", 204

    @app.get("/api/recordings/<int:recording_id>/transcript")
    def get_transcript(recording_id):
        return jsonify(service.get_transcript(recording_id))

    @app.post("/api/recordings/<int:recording_id>/transcribe")
    def transcribe(recording_id):
        return jsonify(service.transcribe(recording_id)), 202

    @app.post("/api/recordings/<int:recording_id>/show-in-folder")
    def show_in_folder(recording_id):
        service.show_in_folder(recording_id)
        return "", 204

    @app.get("/api/recordings/<int:recording_id>/audio")
    def audio(recording_id):
        rec = service.get(recording_id)
        if rec["status"] == "recording":
            raise RecorderError("Recording still in progress.")
        path = service.file_path(rec)
        if not path.exists():
            raise NotFound("Audio file is missing.")
        # conditional=True enables HTTP range requests so the player can seek.
        return send_file(path, conditional=True)

    return app
