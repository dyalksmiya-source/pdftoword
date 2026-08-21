"""
PDFtoWord — conversion backend
--------------------------------
A small Flask API that accepts a PDF upload and returns a converted
.docx file, using pdf2docx for the actual conversion.

Endpoints:
  POST /api/convert   -> multipart/form-data, field name "file"
                          returns the converted .docx as a file download

Run:
  python3 server.py
"""

import os
import uuid
import shutil
import logging
from pathlib import Path

from flask import Flask, request, send_file, jsonify
from flask_cors import CORS
from pdf2docx import Converter

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("pdftoword")

APP_ROOT = Path(__file__).parent
UPLOAD_DIR = APP_ROOT / "tmp_uploads"
OUTPUT_DIR = APP_ROOT / "tmp_outputs"
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

MAX_CONTENT_LENGTH = 25 * 1024 * 1024  # 25 MB, matches the frontend limit

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH
CORS(app)  # allow the static frontend (served from a different origin) to call this API


def cleanup_files(*paths):
    """Best-effort removal of temporary files after a request finishes."""
    for path in paths:
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except OSError as exc:
            logger.warning("Could not remove temp file %s: %s", path, exc)


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/api/convert", methods=["POST"])
def convert():
    if "file" not in request.files:
        return jsonify({"error": "No file provided."}), 400

    uploaded = request.files["file"]

    if uploaded.filename == "":
        return jsonify({"error": "No file selected."}), 400

    if not uploaded.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Please upload a valid PDF file."}), 400

    job_id = uuid.uuid4().hex
    input_path = UPLOAD_DIR / f"{job_id}.pdf"
    output_name = Path(uploaded.filename).stem + ".docx"
    output_path = OUTPUT_DIR / f"{job_id}.docx"

    try:
        uploaded.save(input_path)
        logger.info("Converting %s (%d bytes)", uploaded.filename, input_path.stat().st_size)

        converter = Converter(str(input_path))
        try:
            converter.convert(str(output_path))
        finally:
            converter.close()

        if not output_path.exists():
            raise RuntimeError("Conversion did not produce an output file.")

        response = send_file(
            output_path,
            as_attachment=True,
            download_name=output_name,
            mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        # Clean up after the response has been fully sent
        response.call_on_close(lambda: cleanup_files(input_path, output_path))
        return response

    except Exception as exc:
        logger.exception("Conversion failed for %s", uploaded.filename)
        cleanup_files(input_path, output_path)
        return jsonify({"error": "Something went wrong while converting your file. Please try again."}), 500


if __name__ == "__main__":
port = int(os.environ.get("PORT", 5001))
app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)