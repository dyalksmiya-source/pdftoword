# PDF2Wordly

A PDF → Word converter: static frontend (`index.html` / `style.css` / `script.js`,
served from GitHub Pages at `https://pdf2wordly.com`) plus a secure Flask
backend (`server.py`, hosted on Railway) that authenticates via Supabase,
enforces the Free 24h limit / Pro unlimited rule server-side, and converts
with `pdf2docx` (OCR fallback for scanned PDFs).

## Architecture

| Piece    | Where                                   |
|----------|-----------------------------------------|
| Frontend | GitHub Pages → `https://pdf2wordly.com` |
| Backend  | Railway (Flask, `server.py`, `Dockerfile`) |
| Auth + DB| Supabase (`public.profiles`, RLS, trigger) |
| Payments | Lemon Squeezy (Pro Monthly $2.99)       |
| Webhook  | Supabase Edge Function `lemonsqueezy-webhook` |

## Run it locally

**1. Backend**

```bash
pip install -r requirements.txt
# local .env (or export): SUPABASE_URL, SUPABASE_ANON_KEY, SUPABASE_SERVICE_ROLE_KEY
python3 server.py
```

Starts the API at `http://localhost:5001`. Check it's alive:

```bash
curl http://localhost:5001/api/health
```

**2. Frontend**

```bash
python3 -m http.server 8000
```

Then visit `http://localhost:8000`. `script.js` uses `http://localhost:5001`
automatically on localhost. **Before production, set the real Railway URL:**
edit the production fallback in `resolveBackendUrl()` in `script.js`
(currently `https://REPLACE-WITH-YOUR-RAILWAY-BACKEND-URL.up.railway.app`).

## First-time setup (manual steps, in order)

1. **Supabase SQL** — run `supabase/migrations/001_profiles_billing.sql`
   in the SQL Editor (safe to re-run; preserves existing rows).
2. **Supabase Auth → Google provider** — enable it, add
   `https://pdf2wordly.com` to redirect URLs.
3. **Railway env vars** — `SUPABASE_URL`, `SUPABASE_ANON_KEY`,
   `SUPABASE_SERVICE_ROLE_KEY` (plus optional `EXTRA_CORS_ORIGINS`,
   `OCR_LANGUAGES`). No code change needed besides the frontend backend URL.
4. **Edge Function** — `supabase functions deploy lemonsqueezy-webhook`,
   then set secrets `LEMONSQUEEZY_WEBHOOK_SECRET`,
   `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`.
5. **Lemon Squeezy** — product *PDF2Wordly Pro*, variant
   *PDF2Wordly Pro Monthly* ($2.99/month, buy-link UUID
   `539e71b2-9f13-4f62-89c3-053c690074b9` — already wired in the frontend);
   webhook URL `https://<project-ref>.supabase.co/functions/v1/lemonsqueezy-webhook`
   subscribed to all `subscription_*` events; custom field
   `supabase_user_id` is attached automatically by the checkout code.

## API

- `GET /api/health` — public.
- `GET /api/me` — needs `Authorization: Bearer <supabase access token>`;
  returns display-only `{ plan, subscription_status, current_period_end }`.
- `POST /api/convert` — same auth; multipart field `file`. Machine-readable
  error codes: `AUTH_REQUIRED`, `INVALID_TOKEN`, `PROFILE_NOT_FOUND`,
  `FREE_LIMIT_REACHED` (HTTP 429 + `retry_after_seconds`), `CONVERSION_FAILED`,
  `SERVER_ERROR`.

## Notes

- Max upload size: 25 MB, enforced on both frontend and backend; the backend
  also checks PDF magic bytes and re-opens the file before converting.
- Free quota (1 success / rolling 24h) is claimed atomically in Postgres
  (`claim_free_conversion`, row lock) and rolled back on conversion failure —
  simultaneous requests cannot double-spend, and failures never cost quota.
- Pro = `plan='pro'` **and** `subscription_status` in (`active`,`trialing`),
  evaluated only from the database (`is_user_pro` in `server.py`).
- Converted files and uploads are temporary — deleted right after each
  response is sent (and on every failure path).
- `pdf2docx` handles standard text-based PDFs well. Complex layouts,
  scanned/image-only PDFs, and unusual fonts may convert imperfectly — that's
  a limitation of the library, not the integration.
