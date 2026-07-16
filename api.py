"""
Gantry Controller Dashboard API — supports multiple gantries.

Deploy to Render:
    Build command: pip install -r requirements.txt
    Start command: gunicorn api:app --bind 0.0.0.0:$PORT --timeout 120

All gantry-specific endpoints use a gantry name parameter (query param or JSON field).
The gantry name comes from the "gantry" field in status pushes.

Endpoints:
    GET  /            - Web dashboard
    GET  /health      - Health check
    POST /status      - Update test status (JSON, must include "gantry" field)
    POST /motor_data  - Upload motor data CSV (query param: ?gantry=Gantry+E)
    GET  /api/gantries           - List all known gantries
    GET  /api/status?gantry=X    - Get status for one gantry
    GET  /api/status/all         - Get status for all gantries
    GET  /api/files?gantry=X     - List files for a gantry
    GET  /api/files/<gantry>/<name> - Download a file
    POST /api/request_upload     - Request upload (JSON with "gantry" field)
    GET  /api/pending_requests?gantry=X - Check pending requests (polled by gantry PC)
    POST /api/clear_request      - Clear request (JSON with "gantry" field)
"""
import json
import os
from datetime import datetime

from flask import Flask, request, jsonify, send_from_directory

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(DATA_DIR, exist_ok=True)
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

app = Flask(__name__, static_folder=STATIC_DIR)


def _safe_name(gantry: str) -> str:
    """Sanitize gantry name for use as directory/file name."""
    return gantry.strip().replace(" ", "_").replace("/", "_").replace("..", "")


def _status_dir():
    d = os.path.join(DATA_DIR, "status")
    os.makedirs(d, exist_ok=True)
    return d


def _motor_data_dir(gantry: str):
    d = os.path.join(DATA_DIR, "motor_data", _safe_name(gantry))
    os.makedirs(d, exist_ok=True)
    return d


def _request_dir():
    d = os.path.join(DATA_DIR, "requests")
    os.makedirs(d, exist_ok=True)
    return d


# --- Ingest endpoints (called by gantry PCs) ---

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/status", methods=["POST"])
def update_status():
    data = request.get_json()
    if not data:
        return jsonify({"error": "No JSON body"}), 400
    gantry = data.get("gantry")
    if not gantry:
        return jsonify({"error": "Missing 'gantry' field"}), 400

    filepath = os.path.join(_status_dir(), f"{_safe_name(gantry)}.json")

    # Heartbeat: only update timestamp, preserve existing state
    if data.get("heartbeat"):
        if os.path.exists(filepath):
            with open(filepath) as f:
                existing = json.load(f)
            existing["last_update"] = datetime.now().isoformat()
            with open(filepath, "w") as f:
                json.dump(existing, f)
        return jsonify({"ok": True})

    data["last_update"] = datetime.now().isoformat()
    with open(filepath, "w") as f:
        json.dump(data, f)
    return jsonify({"ok": True})


@app.route("/motor_data", methods=["POST"])
def upload_motor_data():
    if "file" not in request.files:
        return jsonify({"error": "No file in request"}), 400
    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "Empty filename"}), 400
    gantry = request.args.get("gantry") or request.form.get("gantry") or "unknown"
    dest_dir = _motor_data_dir(gantry)
    filepath = os.path.join(dest_dir, file.filename)
    file.save(filepath)
    return jsonify({"ok": True, "filename": file.filename, "gantry": gantry})


# --- Pull request endpoints ---

@app.route("/api/request_upload", methods=["POST"])
def request_upload():
    data = request.get_json() or {}
    gantry = data.get("gantry")
    req_type = data.get("type", "motor_data")
    if not gantry:
        return jsonify({"error": "Missing 'gantry' field"}), 400
    if req_type not in ("motor_data", "controller_log", "cycle_log", "profile", "force_snapshot"):
        return jsonify({"error": "Invalid type"}), 400
    req_data = {
        "requested_at": datetime.now().isoformat(),
        "type": req_type,
        "gantry": gantry,
    }
    filepath = os.path.join(_request_dir(), f"{_safe_name(gantry)}.json")
    with open(filepath, "w") as f:
        json.dump(req_data, f)
    return jsonify({"ok": True, "message": f"{req_type} requested from {gantry}."})


@app.route("/api/pending_requests", methods=["GET"])
def pending_requests():
    gantry = request.args.get("gantry")
    if not gantry:
        return jsonify(None)
    filepath = os.path.join(_request_dir(), f"{_safe_name(gantry)}.json")
    if os.path.exists(filepath):
        with open(filepath) as f:
            return jsonify(json.load(f))
    return jsonify(None)


@app.route("/api/clear_request", methods=["POST"])
def clear_request():
    data = request.get_json() or {}
    gantry = data.get("gantry")
    if not gantry:
        return jsonify({"error": "Missing 'gantry' field"}), 400
    filepath = os.path.join(_request_dir(), f"{_safe_name(gantry)}.json")
    if os.path.exists(filepath):
        os.remove(filepath)
    return jsonify({"ok": True})


# --- Read endpoints (called by browser) ---

@app.route("/api/gantries", methods=["GET"])
def list_gantries():
    gantries = []
    status_dir = _status_dir()
    if os.path.exists(status_dir):
        for f in sorted(os.listdir(status_dir)):
            if f.endswith(".json"):
                name = f.replace(".json", "").replace("_", " ")
                gantries.append(name)
    return jsonify(gantries)


@app.route("/api/status", methods=["GET"])
def get_status_api():
    gantry = request.args.get("gantry")
    if not gantry:
        return get_all_status()
    filepath = os.path.join(_status_dir(), f"{_safe_name(gantry)}.json")
    if os.path.exists(filepath):
        with open(filepath) as f:
            return jsonify(json.load(f))
    return jsonify({"state": "offline", "gantry": gantry})


@app.route("/api/status/all", methods=["GET"])
def get_all_status():
    statuses = {}
    status_dir = _status_dir()
    if os.path.exists(status_dir):
        for f in sorted(os.listdir(status_dir)):
            if f.endswith(".json"):
                with open(os.path.join(status_dir, f)) as fh:
                    statuses[f.replace(".json", "").replace("_", " ")] = json.load(fh)
    return jsonify(statuses)


@app.route("/api/files", methods=["GET"])
def list_files():
    gantry = request.args.get("gantry")
    if not gantry:
        return jsonify([])
    dest_dir = _motor_data_dir(gantry)
    files = []
    if os.path.exists(dest_dir):
        for f in sorted(os.listdir(dest_dir), reverse=True):
            size_kb = os.path.getsize(os.path.join(dest_dir, f)) / 1024
            files.append({"name": f, "size_kb": round(size_kb, 1)})
    return jsonify(files)


@app.route("/api/files/<gantry>/<filename>", methods=["GET"])
def download_file(gantry, filename):
    dest_dir = _motor_data_dir(gantry)
    return send_from_directory(dest_dir, filename, as_attachment=True)


# --- Web dashboard ---

@app.route("/", methods=["GET"])
def dashboard():
    return send_from_directory(STATIC_DIR, "index.html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
