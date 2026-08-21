# PDFtoWord

A PDF → Word converter: static frontend (`index.html` / `style.css` / `script.js`)
plus a small Flask backend (`server.py`) that does the real conversion with
`pdf2docx`.

## Run it

**1. Backend**

```bash
pip install -r requirements.txt
python3 server.py
```

Starts the API at `http://localhost:5001`. Check it's alive:

```bash
curl http://localhost:5001/api/health
```

**2. Frontend**

Open `index.html` directly in a browser, or serve the folder:

```bash
python3 -m http.server 8000
```

Then visit `http://localhost:8000`. The frontend calls the API at the
`API_BASE_URL` constant near the top of `convertPdfToWord()` in `script.js`
— currently `http://localhost:5001`. Change this when you deploy the backend
somewhere else.

## Notes

- Max upload size: 25 MB, enforced on both frontend and backend.
- Converted files and uploads are temporary — deleted from the server right
  after each response is sent.
- `pdf2docx` handles standard text-based PDFs well. Complex layouts,
  scanned/image-only PDFs, and unusual fonts may convert imperfectly — that's
  a limitation of the library, not the integration.
- For production: run behind a real WSGI server (gunicorn/uwsgi), not
  Flask's built-in dev server, and lock down CORS to your actual frontend
  origin instead of allowing all origins.
