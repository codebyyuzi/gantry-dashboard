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
from datetime import datetime, timezone
from functools import wraps

from flask import (
    Flask, request, jsonify, send_from_directory,
    session, redirect, url_for, render_template_string,
)

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(DATA_DIR, exist_ok=True)
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

app = Flask(__name__, static_folder=STATIC_DIR)

# Session cookie signing key — MUST be set in the Render environment.
# Falls back to a random per-process key locally (logs everyone out on restart).
app.secret_key = os.environ.get("SECRET_KEY") or os.urandom(32)

# Browser login password (set DASHBOARD_PASSWORD in Render). Fails CLOSED: if
# unset, every view/API route is refused rather than left open to the internet.
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
if not DASHBOARD_PASSWORD:
    print("ERROR: DASHBOARD_PASSWORD not set — dashboard views are DISABLED "
          "until it is configured.")


def require_login(view):
    """Protect browser/view routes. Ingest routes (gantry PCs) are left open."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not DASHBOARD_PASSWORD:
            return jsonify({"error": "Dashboard login not configured "
                                     "(DASHBOARD_PASSWORD unset)"}), 503
        if session.get("authed"):
            return view(*args, **kwargs)
        # HTML request -> redirect to login; API/XHR -> 401 JSON so the page JS
        # can react (e.g. redirect) instead of rendering the login page as data.
        if request.path.startswith("/api/"):
            return jsonify({"error": "Not authenticated"}), 401
        return redirect(url_for("login", next=request.path))
    return wrapped


_LOGIN_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Gantry Dashboard — Login</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 body{font-family:system-ui,sans-serif;background:#0f172a;color:#e2e8f0;
   display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
 form{background:#1e293b;padding:32px;border-radius:12px;box-shadow:0 8px 24px #0006;
   display:flex;flex-direction:column;gap:14px;min-width:280px}
 h1{font-size:18px;margin:0 0 6px}
 input{padding:10px;border-radius:6px;border:1px solid #334155;background:#0f172a;color:#e2e8f0;font-size:15px}
 button{padding:10px;border:0;border-radius:6px;background:#2563eb;color:#fff;font-size:15px;cursor:pointer}
 button:hover{background:#1d4ed8}
 .err{color:#f87171;font-size:13px;margin:0}
</style></head><body>
<form method="post">
 <h1>Gantry Dashboard</h1>
 {% if error %}<p class="err">{{ error }}</p>{% endif %}
 <input type="password" name="password" placeholder="Password" autofocus required>
 <input type="hidden" name="next" value="{{ next_url }}">
 <button type="submit">Log in</button>
</form></body></html>"""


@app.route("/login", methods=["GET", "POST"])
def login():
    if not DASHBOARD_PASSWORD:
        return jsonify({"error": "Dashboard login not configured "
                                 "(DASHBOARD_PASSWORD unset)"}), 503
    error = None
    if request.method == "POST":
        if request.form.get("password") == DASHBOARD_PASSWORD:
            session["authed"] = True
            dest = request.form.get("next") or url_for("dashboard")
            # Only allow same-site relative redirects.
            if not dest.startswith("/"):
                dest = url_for("dashboard")
            return redirect(dest)
        error = "Incorrect password."
    next_url = request.args.get("next", "")
    return render_template_string(_LOGIN_PAGE, error=error, next_url=next_url)


@app.route("/logout", methods=["GET"])
def logout():
    session.clear()
    return redirect(url_for("login"))


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


def _now_iso() -> str:
    """UTC timestamp with offset, so browsers in any timezone parse it right
    (a naive server-local time made 'unresponsive' detection never fire)."""
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(ts):
    """Parse a stored timestamp; naive (legacy) values are treated as UTC."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# A gantry that hasn't been heard from (status push or heartbeat) in this long
# is shown UNRESPONSIVE. Heartbeats are every ~30 s.
UNRESPONSIVE_AFTER_S = 120
# A "running" gantry whose cycle count hasn't advanced in
# max(STALL_MIN_S, STALL_CYCLE_FACTOR * cycle_time) is shown STALLED — the app
# is alive (heartbeating) but the test isn't making progress.
STALL_MIN_S = 180
STALL_CYCLE_FACTOR = 10

# Fields a live heartbeat may update (anything else in the stored status, e.g.
# an earlier error message, is kept unless the new state clears it).
_LIVE_FIELDS = ("state", "current_cycle", "target_cycles", "progress_percent",
                "cycle_time", "profile", "error", "estop")


def _with_progress(existing: dict, new: dict, now: str) -> dict:
    """Carry/refresh last_progress: the time the cycle count last changed (or
    the state changed). Used for stall detection."""
    changed = (new.get("state") != existing.get("state")
               or new.get("current_cycle") != existing.get("current_cycle"))
    new["last_progress"] = now if changed or not existing.get("last_progress") \
        else existing["last_progress"]
    return new


def _annotate(status: dict) -> dict:
    """Add server-computed health fields for the browser:
    age_s (since last contact), reported_state, and an effective `state`
    that is 'unresponsive' / 'stalled' when appropriate."""
    s = dict(status)
    now = datetime.now(timezone.utc)
    reported = s.get("state", "offline")
    s["reported_state"] = reported
    last = _parse_ts(s.get("last_update"))
    if last is not None:
        s["age_s"] = round((now - last).total_seconds(), 1)
    if reported in ("disconnected", "offline"):
        return s
    if last is not None and s["age_s"] > UNRESPONSIVE_AFTER_S:
        s["state"] = "unresponsive"
        return s
    if reported == "running":
        prog = _parse_ts(s.get("last_progress"))
        cycle_time = s.get("cycle_time") or 0
        limit = max(STALL_MIN_S, STALL_CYCLE_FACTOR * float(cycle_time))
        if prog is not None:
            since = (now - prog).total_seconds()
            s["since_progress_s"] = round(since, 1)
            if since > limit:
                s["state"] = "stalled"
    return s


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
    now = _now_iso()
    existing = {}
    if os.path.exists(filepath):
        try:
            with open(filepath) as f:
                existing = json.load(f)
        except (OSError, json.JSONDecodeError):
            existing = {}

    if data.get("heartbeat"):
        if "state" not in data:
            # Legacy heartbeat (older app): only refresh the timestamp.
            if existing:
                existing["last_update"] = now
                with open(filepath, "w") as f:
                    json.dump(existing, f)
            return jsonify({"ok": True})
        # Live heartbeat: the app's CURRENT state wins, so a lost status push
        # (e.g. the 'error' after a fault) is corrected within one heartbeat.
        merged = dict(existing)
        for k in _LIVE_FIELDS:
            if k in data:
                merged[k] = data[k]
        if data.get("state") != "error":
            merged.pop("error", None)
        merged["gantry"] = gantry
        data = merged

    data.pop("heartbeat", None)
    data = _with_progress(existing, data, now)
    data["last_update"] = now
    with open(filepath, "w") as f:
        json.dump(data, f)
    return jsonify({"ok": True})


def _save_uploaded_file(file, gantry: str):
    """Save an uploaded file into the gantry's motor-data dir. Handles gzip
    (.gz filename -> decompress + strip suffix) and guards path traversal.
    Returns the stored filename."""
    dest_dir = _motor_data_dir(gantry)
    filename = os.path.basename(file.filename)  # guard against path traversal
    if filename.endswith(".gz"):
        import gzip
        raw = gzip.decompress(file.read())
        filename = filename[:-3]
        with open(os.path.join(dest_dir, filename), "wb") as f:
            f.write(raw)
    else:
        file.save(os.path.join(dest_dir, filename))
    return filename


@app.route("/motor_data", methods=["POST"])
def upload_motor_data():
    if "file" not in request.files:
        return jsonify({"error": "No file in request"}), 400
    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "Empty filename"}), 400
    gantry = request.args.get("gantry") or request.form.get("gantry") or "unknown"
    filename = _save_uploaded_file(file, gantry)
    return jsonify({"ok": True, "filename": filename, "gantry": gantry})


@app.route("/api/upload_local", methods=["POST"])
@require_login
def upload_local_file():
    """Browser-side import: upload a CSV from the user's PC into a gantry's
    file list. Authenticated (unlike the gantry-PC /motor_data ingest route)."""
    if "file" not in request.files:
        return jsonify({"error": "No file in request"}), 400
    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "Empty filename"}), 400
    gantry = request.form.get("gantry")
    if not gantry:
        return jsonify({"error": "Missing 'gantry' field"}), 400
    filename = _save_uploaded_file(file, gantry)
    return jsonify({"ok": True, "filename": filename, "gantry": gantry})


# --- Pull request endpoints ---

@app.route("/api/request_upload", methods=["POST"])
@require_login
def request_upload():
    data = request.get_json() or {}
    gantry = data.get("gantry")
    req_type = data.get("type", "motor_data")
    if not gantry:
        return jsonify({"error": "Missing 'gantry' field"}), 400
    if req_type not in ("motor_data", "controller_log", "cycle_log", "profile", "force_snapshot"):
        return jsonify({"error": "Invalid type"}), 400
    req_data = {
        "requested_at": _now_iso(),
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
@require_login
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
@require_login
def get_status_api():
    gantry = request.args.get("gantry")
    if not gantry:
        return get_all_status()
    filepath = os.path.join(_status_dir(), f"{_safe_name(gantry)}.json")
    if os.path.exists(filepath):
        with open(filepath) as f:
            return jsonify(_annotate(json.load(f)))
    return jsonify({"state": "offline", "gantry": gantry})


@app.route("/api/status/all", methods=["GET"])
@require_login
def get_all_status():
    statuses = {}
    status_dir = _status_dir()
    if os.path.exists(status_dir):
        for f in sorted(os.listdir(status_dir)):
            if f.endswith(".json"):
                with open(os.path.join(status_dir, f)) as fh:
                    statuses[f.replace(".json", "").replace("_", " ")] = _annotate(json.load(fh))
    return jsonify(statuses)


@app.route("/api/files", methods=["GET"])
@require_login
def list_files():
    gantry = request.args.get("gantry")
    if not gantry:
        return jsonify([])
    dest_dir = _motor_data_dir(gantry)
    files = []
    if os.path.exists(dest_dir):
        for f in sorted(os.listdir(dest_dir), reverse=True):
            fpath = os.path.join(dest_dir, f)
            size_kb = os.path.getsize(fpath) / 1024
            # mtime lets the client detect a file being overwritten/refreshed
            # (same name + count) — not just brand-new files.
            files.append({
                "name": f,
                "size_kb": round(size_kb, 1),
                "mtime": os.path.getmtime(fpath),
            })
    return jsonify(files)


@app.route("/api/files/<gantry>/<filename>", methods=["GET"])
@require_login
def download_file(gantry, filename):
    dest_dir = _motor_data_dir(gantry)
    return send_from_directory(dest_dir, filename, as_attachment=True)


@app.route("/api/files/<gantry>/<filename>", methods=["DELETE"])
@require_login
def delete_file(gantry, filename):
    """Delete one file from a gantry's motor-data dir. Authenticated. The
    resolved path is verified to stay inside the gantry dir to block traversal
    (e.g. '../../') from deleting arbitrary files."""
    dest_dir = os.path.realpath(_motor_data_dir(gantry))
    # Strip any path components from the name, then resolve and re-check.
    target = os.path.realpath(os.path.join(dest_dir, os.path.basename(filename)))
    if os.path.dirname(target) != dest_dir:
        return jsonify({"error": "Invalid path"}), 400
    if not os.path.isfile(target):
        return jsonify({"error": "File not found"}), 404
    try:
        os.remove(target)
        print(f"DELETED file: {gantry}/{os.path.basename(target)}")
        return jsonify({"ok": True, "deleted": os.path.basename(target), "gantry": gantry})
    except OSError as e:
        return jsonify({"error": str(e)}), 500


# --- Web dashboard ---

@app.route("/", methods=["GET"])
@require_login
def dashboard():
    return send_from_directory(STATIC_DIR, "index.html")


if __name__ == "__main__":
    # Local dev only (Render runs gunicorn via the Procfile). Loopback by default.
    port = int(os.environ.get("PORT", 5000))
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=port)
