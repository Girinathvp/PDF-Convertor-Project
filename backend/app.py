"""
app.py - Flask API for PDF bank statement -> Excel conversion.

Endpoints:
  POST /api/upload            -> save file, count pages, return TAT estimate
  POST /api/process/<job_id>  -> kick off background conversion (merge_pages flag)
  GET  /api/status/<job_id>   -> poll progress {status, pages_done, total_pages}
  GET  /api/download/<job_id> -> download the resulting .xlsx

Large files / long-running work are handled with a background thread per job
(not the request thread), so uploads and processing never block on Flask's
request timeout. Page-by-page progress is reported back for the loading UI.
"""

import os
import uuid
import threading
import time
from functools import wraps

from flask import Flask, request, jsonify, send_file, render_template, Response
from werkzeug.utils import secure_filename

from extractor import get_page_count, estimate_tat, process_pdf, write_excel

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
OUTPUT_DIR = os.path.join(BASE_DIR, "outputs")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

app = Flask(
    __name__,
    static_folder=os.path.join(BASE_DIR, "..", "frontend"),
    static_url_path="/static",
    template_folder=os.path.join(BASE_DIR, "..", "frontend"),
)

# Accept large statements (up to 300 MB). Tune to your environment.
app.config["MAX_CONTENT_LENGTH"] = 300 * 1024 * 1024

jobs = {}
jobs_lock = threading.Lock()

JOB_TTL_SECONDS = 3600  # simple cleanup horizon for old jobs/files


# ---------------------------------------------------------------------------
# Optional HTTP Basic Auth gate
#
# Since this app handles bank statement data, an "unlisted URL" alone is
# NOT real access control (URLs leak via browser history, referrer headers,
# screenshots, forwarded emails, server/proxy logs). Set APP_USERNAME and
# APP_PASSWORD as environment variables to require a login on every route.
# Leave them unset to run with no auth gate (fine only for a private
# network / VPN-restricted environment).
# ---------------------------------------------------------------------------

APP_USERNAME = os.environ.get("APP_USERNAME")
APP_PASSWORD = os.environ.get("APP_PASSWORD")


def _auth_required():
    return bool(APP_USERNAME and APP_PASSWORD)


def _check_auth(auth):
    return auth and auth.username == APP_USERNAME and auth.password == APP_PASSWORD


@app.before_request
def _require_login():
    if not _auth_required():
        return None
    auth = request.authorization
    if not _check_auth(auth):
        return Response(
            "Authentication required.",
            401,
            {"WWW-Authenticate": 'Basic realm="Bank Statement Converter"'},
        )
    return None


# ---------------------------------------------------------------------------
# Static / index
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/robots.txt")
def robots():
    return app.send_static_file("robots.txt")


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

@app.route("/api/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "Empty filename"}), 400
    if not file.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Only PDF files are supported"}), 400

    job_id = str(uuid.uuid4())
    filename = secure_filename(file.filename)
    saved_path = os.path.join(UPLOAD_DIR, f"{job_id}_{filename}")
    file.save(saved_path)

    try:
        num_pages = get_page_count(saved_path)
    except Exception as e:
        os.remove(saved_path)
        return jsonify({"error": f"Could not read PDF: {e}"}), 400

    tat_seconds = estimate_tat(num_pages)

    with jobs_lock:
        jobs[job_id] = {
            "status": "uploaded",
            "created_at": time.time(),
            "file_path": saved_path,
            "total_pages": num_pages,
            "pages_done": 0,
            "tat_seconds": tat_seconds,
            "output_path": None,
            "error": None,
        }

    return jsonify(
        {
            "job_id": job_id,
            "num_pages": num_pages,
            "estimated_seconds": tat_seconds,
        }
    )


# ---------------------------------------------------------------------------
# Background processing
# ---------------------------------------------------------------------------

def _run_job(job_id, merge_pages):
    with jobs_lock:
        job = jobs[job_id]
        job["status"] = "processing"
        pdf_path = job["file_path"]

    def progress_cb(done, total):
        with jobs_lock:
            jobs[job_id]["pages_done"] = done

    try:
        results, methods = process_pdf(pdf_path, progress_callback=progress_cb)
        output_path = os.path.join(OUTPUT_DIR, f"{job_id}.xlsx")
        write_excel(results, output_path, merge_pages=merge_pages)
        with jobs_lock:
            jobs[job_id]["status"] = "done"
            jobs[job_id]["output_path"] = output_path
            jobs[job_id]["methods"] = methods
    except Exception as e:
        with jobs_lock:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = str(e)


@app.route("/api/process/<job_id>", methods=["POST"])
def process(job_id):
    with jobs_lock:
        if job_id not in jobs:
            return jsonify({"error": "Unknown job_id"}), 404
        if jobs[job_id]["status"] == "processing":
            return jsonify({"error": "Already processing"}), 400

    data = request.get_json(silent=True) or {}
    merge_pages = bool(data.get("merge_pages", True))

    thread = threading.Thread(target=_run_job, args=(job_id, merge_pages), daemon=True)
    thread.start()

    return jsonify({"status": "started"})


@app.route("/api/status/<job_id>", methods=["GET"])
def status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify({"error": "Unknown job_id"}), 404
        return jsonify(
            {
                "status": job["status"],
                "pages_done": job["pages_done"],
                "total_pages": job["total_pages"],
                "estimated_seconds": job["tat_seconds"],
                "error": job["error"],
            }
        )


@app.route("/api/download/<job_id>", methods=["GET"])
def download(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "File not ready"}), 400
    return send_file(
        job["output_path"],
        as_attachment=True,
        download_name="converted_statement.xlsx",
    )


# ---------------------------------------------------------------------------
# Cleanup: delete uploaded PDFs / generated Excel files after JOB_TTL_SECONDS.
# These contain real account/transaction data, so we don't want them sitting
# on disk indefinitely on a shared server.
# ---------------------------------------------------------------------------

def _cleanup_loop():
    while True:
        time.sleep(300)
        cutoff = time.time() - JOB_TTL_SECONDS
        with jobs_lock:
            expired = [jid for jid, j in jobs.items() if j["created_at"] < cutoff]
            for jid in expired:
                job = jobs.pop(jid)
                for path in (job.get("file_path"), job.get("output_path")):
                    if path and os.path.exists(path):
                        try:
                            os.remove(path)
                        except OSError:
                            pass


threading.Thread(target=_cleanup_loop, daemon=True).start()


if __name__ == "__main__":
    # threaded=True lets Flask's dev server handle status polling while a
    # background job is running. In production this file isn't run directly
    # -- gunicorn imports `app` instead (see Dockerfile / README).
    debug_mode = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(debug=debug_mode, threaded=True, port=int(os.environ.get("PORT", 5000)), host="0.0.0.0")
