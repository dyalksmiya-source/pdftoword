"""
PDFtoWord — conversion backend
--------------------------------
A Flask API that accepts a PDF upload and returns a converted .docx file.

Strategy:
  1. Try pdf2docx first (fast, preserves layout for normal text PDFs).
  2. Check how much real text pdf2docx actually pulled out using PyMuPDF.
     If a page has (almost) no extractable text, the PDF is likely
     scanned/image-only, so pdf2docx alone would give an empty/broken result.
  3. In that case, fall back to OCR: render each page to an image and run
     Tesseract on it, then build a plain .docx with the recognized text.

Endpoints:
  GET  /api/health    -> health check
  POST /api/convert   -> multipart/form-data, field name "file"
                          returns the converted .docx as a file download

Run:
  python3 server.py
"""

import os
import uuid
import logging
from pathlib import Path

from flask import Flask, request, send_file, jsonify
from flask_cors import CORS
from pdf2docx import Converter
import fitz  # PyMuPDF
import pytesseract
from PIL import Image
from docx import Document

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("pdftoword")

APP_ROOT = Path(__file__).parent
UPLOAD_DIR = APP_ROOT / "tmp_uploads"
OUTPUT_DIR = APP_ROOT / "tmp_outputs"
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

MAX_CONTENT_LENGTH = 25 * 1024 * 1024  # 25 MB, matches the frontend limit

# How much text (characters) per page counts as "this page has real text".
# Below this threshold across most pages, we treat the PDF as scanned.
MIN_CHARS_PER_PAGE = 20

# OCR render resolution. Higher = better accuracy, slower, more memory.
OCR_ZOOM = 2.0

# Languages Tesseract should look for. Add more codes if needed, e.g. "eng+fra+ara".
OCR_LANGUAGES = os.environ.get("OCR_LANGUAGES", "eng+fra+ara")

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


def pdf_is_scanned(pdf_path: Path) -> bool:
    """Heuristic: open the PDF with PyMuPDF and check how much extractable
    text each page has. If most pages have almost none, it's likely a
    scanned/image-only PDF that pdf2docx can't handle properly."""
    doc = fitz.open(pdf_path)
    try:
        if doc.page_count == 0:
            return False
        low_text_pages = 0
        for page in doc:
            text = page.get_text().strip()
            if len(text) < MIN_CHARS_PER_PAGE:
                low_text_pages += 1
        return (low_text_pages / doc.page_count) >= 0.6
    finally:
        doc.close()


def ocr_pdf_to_docx(pdf_path: Path, output_path: Path) -> None:
    """Render each page as an image and run Tesseract OCR on it, then
    write the recognized text into a .docx, one section per page."""
    doc = fitz.open(pdf_path)
    document = Document()
    try:
        matrix = fitz.Matrix(OCR_ZOOM, OCR_ZOOM)
        for i, page in enumerate(doc):
            pix = page.get_pixmap(matrix=matrix)
            img_path = pdf_path.with_suffix(f".page{i}.png")
            pix.save(img_path)
            try:
                with Image.open(img_path) as img:
                    text = pytesseract.image_to_string(img, lang=OCR_LANGUAGES)
            finally:
                cleanup_files(img_path)

            if i > 0:
                document.add_page_break()
            for line in text.splitlines():
                document.add_paragraph(line)
    finally:
        doc.close()

    document.save(output_path)


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

        if pdf_is_scanned(input_path):
            logger.info("Detected scanned/image-only PDF, using OCR path")
            ocr_pdf_to_docx(input_path, output_path)
        else:
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
