"""
Document Intelligence Agent - Backend Server
Powered by Sarvam AI Doc-AI APIs
"""

import os
import json
import time
import uuid
import threading
from datetime import datetime
from pathlib import Path

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import requests

# ─── Configuration ───────────────────────────────────────────────────────────
SARVAM_API_KEY = "sk_9bdm1dyl_P8dGzn8w6sz9fTK4bpREo1kM"
SARVAM_BASE_URL = "https://api.sarvam.ai"
HEADERS = {"api-subscription-key": SARVAM_API_KEY}

app = Flask(__name__, static_folder="static", static_url_path="")
CORS(app)

# Storage directories
UPLOAD_DIR = Path("uploads")
DATA_DIR = Path("data")
UPLOAD_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)

# In-memory stores
documents_db = {}  # doc_id -> document metadata + results
workflows_db = {}  # workflow rules
alerts_db = []     # triggered alerts


# ─── Sarvam API Helpers ──────────────────────────────────────────────────────

def sarvam_digitise(file_path: str, language: str = "en-IN") -> dict:
    """Submit a document for OCR digitisation via Sarvam Doc-AI."""
    url = f"{SARVAM_BASE_URL}/doc-ai/v1/job/digitise"
    with open(file_path, "rb") as f:
        files = {"file": (os.path.basename(file_path), f)}
        data = {"language": language, "output_format": "json"}
        resp = requests.post(url, headers=HEADERS, files=files, data=data, timeout=60)
    resp.raise_for_status()
    return resp.json()


def sarvam_extract(file_path: str, schema: dict, language: str = "en-IN") -> dict:
    """Submit a document for schema-based extraction via Sarvam Doc-AI."""
    url = f"{SARVAM_BASE_URL}/doc-ai/v1/job/extract"
    with open(file_path, "rb") as f:
        files = {"file": (os.path.basename(file_path), f)}
        data = {
            "language": language,
            "output_format": "json",
            "schema": json.dumps(schema),
        }
        resp = requests.post(url, headers=HEADERS, files=files, data=data, timeout=60)
    resp.raise_for_status()
    return resp.json()


def sarvam_job_status(job_id: str) -> dict:
    """Poll the status of a Sarvam Doc-AI job."""
    url = f"{SARVAM_BASE_URL}/doc-ai/v1/job/{job_id}/status"
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json()


def sarvam_job_results(job_id: str) -> dict:
    """Fetch results of a completed Sarvam Doc-AI job."""
    url = f"{SARVAM_BASE_URL}/doc-ai/v1/job/{job_id}/results"
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json()


def poll_and_store(doc_id: str, job_id: str, job_type: str):
    """Background thread: poll job status until terminal, then store results."""
    terminal = {"completed", "partially_completed", "failed", "rejected"}
    max_attempts = 60
    for _ in range(max_attempts):
        time.sleep(3)
        try:
            status_resp = sarvam_job_status(job_id)
            status = status_resp.get("status", "unknown")
            documents_db[doc_id]["job_status"] = status
            documents_db[doc_id]["usage"] = status_resp.get("usage", {})
            if status in terminal:
                break
        except Exception as e:
            documents_db[doc_id]["job_status"] = f"poll_error: {str(e)}"
            break

    # Fetch results
    if documents_db[doc_id].get("job_status") in ("completed", "partially_completed"):
        try:
            results = sarvam_job_results(job_id)
            documents_db[doc_id]["results"] = results
            documents_db[doc_id]["processed_at"] = datetime.now().isoformat()

            # Check workflow rules
            check_workflow_alerts(doc_id)
        except Exception as e:
            documents_db[doc_id]["results_error"] = str(e)
    elif documents_db[doc_id].get("job_status") not in terminal:
        documents_db[doc_id]["job_status"] = "timeout"


def check_workflow_alerts(doc_id: str):
    """Check extracted data against workflow rules and trigger alerts."""
    doc = documents_db.get(doc_id, {})
    results = doc.get("results", {})

    for wf_id, rule in workflows_db.items():
        field = rule.get("field", "")
        condition = rule.get("condition", "contains")
        value = rule.get("value", "")
        action = rule.get("action", "alert")

        # Search results for matching field
        results_str = json.dumps(results).lower()
        triggered = False

        if condition == "contains" and value.lower() in results_str:
            triggered = True
        elif condition == "greater_than":
            # Try to find numeric values
            try:
                for key, val in _flatten_dict(results).items():
                    if field.lower() in key.lower():
                        if float(val) > float(value):
                            triggered = True
                            break
            except (ValueError, TypeError):
                pass
        elif condition == "equals":
            for key, val in _flatten_dict(results).items():
                if field.lower() in key.lower() and str(val).lower() == value.lower():
                    triggered = True
                    break

        if triggered:
            alert = {
                "id": str(uuid.uuid4())[:8],
                "timestamp": datetime.now().isoformat(),
                "document_id": doc_id,
                "document_name": doc.get("filename", "Unknown"),
                "rule_name": rule.get("name", "Unnamed Rule"),
                "action": action,
                "message": f"Rule '{rule.get('name')}' triggered on document '{doc.get('filename')}'",
                "read": False,
            }
            alerts_db.append(alert)


def _flatten_dict(d, parent_key="", sep="."):
    """Flatten a nested dictionary."""
    items = []
    if isinstance(d, dict):
        for k, v in d.items():
            new_key = f"{parent_key}{sep}{k}" if parent_key else k
            if isinstance(v, dict):
                items.extend(_flatten_dict(v, new_key, sep).items())
            elif isinstance(v, list):
                for i, item in enumerate(v):
                    items.extend(_flatten_dict(item, f"{new_key}[{i}]", sep).items())
            else:
                items.append((new_key, v))
    return dict(items)


# ─── API Routes ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/upload", methods=["POST"])
def upload_document():
    """Upload a document for processing."""
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "Empty filename"}), 400

    doc_id = str(uuid.uuid4())[:8]
    ext = os.path.splitext(file.filename)[1]
    saved_path = UPLOAD_DIR / f"{doc_id}{ext}"
    file.save(str(saved_path))

    process_type = request.form.get("process_type", "digitise")
    language = request.form.get("language", "en-IN")
    schema_str = request.form.get("schema", "")

    doc_entry = {
        "id": doc_id,
        "filename": file.filename,
        "file_path": str(saved_path),
        "uploaded_at": datetime.now().isoformat(),
        "process_type": process_type,
        "language": language,
        "job_id": None,
        "job_status": "submitting",
        "results": None,
        "usage": {},
    }
    documents_db[doc_id] = doc_entry

    try:
        if process_type == "extract" and schema_str:
            schema = json.loads(schema_str)
            job_resp = sarvam_extract(str(saved_path), schema, language)
        else:
            job_resp = sarvam_digitise(str(saved_path), language)

        job_id = job_resp.get("job_id") or job_resp.get("id")
        documents_db[doc_id]["job_id"] = job_id
        documents_db[doc_id]["job_status"] = "processing"

        # Start background polling
        thread = threading.Thread(
            target=poll_and_store, args=(doc_id, job_id, process_type), daemon=True
        )
        thread.start()

    except requests.exceptions.HTTPError as e:
        error_detail = ""
        try:
            error_detail = e.response.json()
        except Exception:
            error_detail = e.response.text if e.response else str(e)
        documents_db[doc_id]["job_status"] = "submission_failed"
        documents_db[doc_id]["error"] = str(error_detail)
        return jsonify({"error": str(error_detail), "doc_id": doc_id}), 500
    except Exception as e:
        documents_db[doc_id]["job_status"] = "submission_failed"
        documents_db[doc_id]["error"] = str(e)
        return jsonify({"error": str(e), "doc_id": doc_id}), 500

    return jsonify({"doc_id": doc_id, "status": "processing", "job_id": job_id})


@app.route("/api/documents", methods=["GET"])
def list_documents():
    """List all processed documents."""
    docs = []
    for doc_id, doc in documents_db.items():
        docs.append({
            "id": doc["id"],
            "filename": doc["filename"],
            "uploaded_at": doc["uploaded_at"],
            "process_type": doc["process_type"],
            "job_status": doc["job_status"],
            "processed_at": doc.get("processed_at"),
            "has_results": doc.get("results") is not None,
        })
    return jsonify(docs)


@app.route("/api/documents/<doc_id>", methods=["GET"])
def get_document(doc_id):
    """Get a single document with its results."""
    doc = documents_db.get(doc_id)
    if not doc:
        return jsonify({"error": "Document not found"}), 404
    return jsonify(doc)


@app.route("/api/documents/<doc_id>/status", methods=["GET"])
def get_document_status(doc_id):
    """Get the processing status of a document."""
    doc = documents_db.get(doc_id)
    if not doc:
        return jsonify({"error": "Document not found"}), 404
    return jsonify({
        "id": doc["id"],
        "job_status": doc["job_status"],
        "usage": doc.get("usage", {}),
        "has_results": doc.get("results") is not None,
    })


@app.route("/api/analytics", methods=["GET"])
def get_analytics():
    """Get analytics / dashboard data from all processed documents."""
    total = len(documents_db)
    completed = sum(1 for d in documents_db.values() if d.get("job_status") == "completed")
    failed = sum(1 for d in documents_db.values() if d.get("job_status") in ("failed", "submission_failed"))
    processing = sum(1 for d in documents_db.values() if d.get("job_status") == "processing")

    total_pages = 0
    doc_types = {}
    languages = {}
    timeline = {}

    for doc in documents_db.values():
        # Pages
        usage = doc.get("usage", {})
        total_pages += usage.get("pages_processed", 0)

        # File types
        ext = os.path.splitext(doc["filename"])[1].lower() or ".unknown"
        doc_types[ext] = doc_types.get(ext, 0) + 1

        # Languages
        lang = doc.get("language", "en-IN")
        languages[lang] = languages.get(lang, 0) + 1

        # Timeline (by date)
        date_key = doc["uploaded_at"][:10]
        timeline[date_key] = timeline.get(date_key, 0) + 1

    return jsonify({
        "summary": {
            "total_documents": total,
            "completed": completed,
            "failed": failed,
            "processing": processing,
            "total_pages": total_pages,
        },
        "doc_types": doc_types,
        "languages": languages,
        "timeline": timeline,
        "alerts_count": len([a for a in alerts_db if not a.get("read")]),
    })


# ─── Workflow Rules ──────────────────────────────────────────────────────────

@app.route("/api/workflows", methods=["GET"])
def list_workflows():
    return jsonify(list(workflows_db.values()))


@app.route("/api/workflows", methods=["POST"])
def create_workflow():
    data = request.json
    wf_id = str(uuid.uuid4())[:8]
    rule = {
        "id": wf_id,
        "name": data.get("name", "Unnamed Rule"),
        "field": data.get("field", ""),
        "condition": data.get("condition", "contains"),
        "value": data.get("value", ""),
        "action": data.get("action", "alert"),
        "created_at": datetime.now().isoformat(),
        "active": True,
    }
    workflows_db[wf_id] = rule
    return jsonify(rule), 201


@app.route("/api/workflows/<wf_id>", methods=["DELETE"])
def delete_workflow(wf_id):
    if wf_id in workflows_db:
        del workflows_db[wf_id]
        return jsonify({"deleted": True})
    return jsonify({"error": "Not found"}), 404


# ─── Alerts ──────────────────────────────────────────────────────────────────

@app.route("/api/alerts", methods=["GET"])
def list_alerts():
    return jsonify(alerts_db)


@app.route("/api/alerts/<alert_id>/read", methods=["POST"])
def mark_alert_read(alert_id):
    for alert in alerts_db:
        if alert["id"] == alert_id:
            alert["read"] = True
            return jsonify(alert)
    return jsonify({"error": "Not found"}), 404


@app.route("/api/alerts/clear", methods=["POST"])
def clear_alerts():
    alerts_db.clear()
    return jsonify({"cleared": True})


# ─── Export ──────────────────────────────────────────────────────────────────

@app.route("/api/export/<doc_id>", methods=["GET"])
def export_document(doc_id):
    """Export document results as JSON (for BI tools)."""
    doc = documents_db.get(doc_id)
    if not doc:
        return jsonify({"error": "Document not found"}), 404

    export_data = {
        "document_id": doc["id"],
        "filename": doc["filename"],
        "processed_at": doc.get("processed_at"),
        "process_type": doc["process_type"],
        "language": doc["language"],
        "results": doc.get("results"),
    }

    fmt = request.args.get("format", "json")
    if fmt == "csv":
        # Flatten results for CSV
        flat = _flatten_dict(doc.get("results", {}))
        lines = ["field,value"]
        for k, v in flat.items():
            lines.append(f'"{k}","{v}"')
        csv_content = "\n".join(lines)
        return csv_content, 200, {
            "Content-Type": "text/csv",
            "Content-Disposition": f"attachment; filename={doc_id}_export.csv",
        }

    return jsonify(export_data)


# ─── Run ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("  [*] Document Intelligence Agent")
    print("  Powered by Sarvam AI")
    print("=" * 60)
    print("  => Open http://localhost:5000 in your browser")
    print("=" * 60 + "\n")
    app.run(host="0.0.0.0", port=5000, debug=True)
