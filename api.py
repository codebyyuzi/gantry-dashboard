"""
Gantry Controller Dashboard API.

Receives status/data from the gantry PC and serves a web dashboard.

Deploy to Render:
    Build command: pip install -r requirements.txt
    Start command: gunicorn api:app --bind 0.0.0.0:$PORT --timeout 120

Endpoints:
    GET  /            - Web dashboard (HTML + Plotly charts)
    GET  /health      - Health check
    POST /status      - Update test status (JSON)
    POST /motor_data  - Upload motor data CSV
    GET  /api/status  - Get current status (JSON)
    GET  /api/files   - List uploaded files (JSON)
    GET  /api/files/<name> - Download a file
    POST /api/request_upload  - Request gantry PC to upload latest data
    GET  /api/pending_requests - Check for pending upload requests (polled by gantry PC)
    POST /api/clear_request   - Clear pending request (called by gantry PC after upload)
"""
import json
import os
from datetime import datetime

from flask import Flask, request, jsonify, send_from_directory

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(DATA_DIR, exist_ok=True)
STATUS_FILE = os.path.join(DATA_DIR, "status.json")
REQUEST_FILE = os.path.join(DATA_DIR, "pending_request.json")
MOTOR_DATA_DIR = os.path.join(DATA_DIR, "motor_data")
os.makedirs(MOTOR_DATA_DIR, exist_ok=True)
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

app = Flask(__name__, static_folder=STATIC_DIR)


# --- Ingest endpoints (called by gantry PC) ---

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/status", methods=["POST"])
def update_status():
    data = request.get_json()
    if not data:
        return jsonify({"error": "No JSON body"}), 400
    data["last_update"] = datetime.now().isoformat()
    with open(STATUS_FILE, "w") as f:
        json.dump(data, f)
    return jsonify({"ok": True})


@app.route("/motor_data", methods=["POST"])
def upload_motor_data():
    if "file" not in request.files:
        return jsonify({"error": "No file in request"}), 400
    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "Empty filename"}), 400
    filepath = os.path.join(MOTOR_DATA_DIR, file.filename)
    file.save(filepath)
    return jsonify({"ok": True, "filename": file.filename})


# --- Pull request endpoints ---

@app.route("/api/request_upload", methods=["POST"])
def request_upload():
    """Dashboard user requests the gantry PC to upload latest data."""
    req_data = {
        "requested_at": datetime.now().isoformat(),
        "type": "motor_data",
    }
    with open(REQUEST_FILE, "w") as f:
        json.dump(req_data, f)
    return jsonify({"ok": True, "message": "Upload requested. Gantry PC will respond within 30s."})


@app.route("/api/pending_requests", methods=["GET"])
def pending_requests():
    """Polled by gantry PC to check if dashboard wants data."""
    if os.path.exists(REQUEST_FILE):
        with open(REQUEST_FILE) as f:
            return jsonify(json.load(f))
    return jsonify(None)


@app.route("/api/clear_request", methods=["POST"])
def clear_request():
    """Called by gantry PC after fulfilling the upload request."""
    if os.path.exists(REQUEST_FILE):
        os.remove(REQUEST_FILE)
    return jsonify({"ok": True})


# --- Read endpoints (called by browser) ---

@app.route("/api/status", methods=["GET"])
def get_status_api():
    if os.path.exists(STATUS_FILE):
        with open(STATUS_FILE) as f:
            return jsonify(json.load(f))
    return jsonify({"state": "offline"})


@app.route("/api/files", methods=["GET"])
def list_files():
    files = []
    if os.path.exists(MOTOR_DATA_DIR):
        for f in sorted(os.listdir(MOTOR_DATA_DIR), reverse=True):
            if f.endswith(".csv"):
                size_kb = os.path.getsize(os.path.join(MOTOR_DATA_DIR, f)) / 1024
                files.append({"name": f, "size_kb": round(size_kb, 1)})
    return jsonify(files)


@app.route("/api/files/<filename>", methods=["GET"])
def download_file(filename):
    return send_from_directory(MOTOR_DATA_DIR, filename, as_attachment=True)


# --- Web dashboard ---

@app.route("/", methods=["GET"])
def dashboard():
    return send_from_directory(STATIC_DIR, "index.html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
