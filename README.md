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
| Payments | Paddle Billing, Merchant of Record (Pro Monthly $4.99): `billing.js` + `billing.py`, live |

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
4. **Payments (Paddle)** — run `docs/sql/002_paddle_billing.sql` (WORD repo)
   first, then set `PADDLE_ENV`, `PADDLE_API_KEY`, `PADDLE_WEBHOOK_SECRET`,
   `PADDLE_PRICE_ID` in Railway (never in files). Webhook destination:
   `https://api.pdf2wordly.com/api/paddle/webhook`. Live: `PADDLE_ENV=production`
   (the server then calls `https://api.paddle.com`). The checkout is open to
   everyone; client-side token, price ID and environment are at the top of
   `billing.js` (the one place to switch between live and sandbox).
5. **Site values** — legal name and legal form, support email, price, refund
   days and reply time live in `_tools/site-config.json`; run `python _tools/apply_site_config.py`
   after changing them.

## API

- `GET /api/health` — public.
- `GET /api/me` — needs `Authorization: Bearer <supabase access token>`;
  returns display-only `{ plan, subscription_status, current_period_end,
  renews_at, ends_at, cancel_scheduled, billing_source, can_purchase,
  pro_daily_limit }`. `can_purchase` is false after an approved refund (one
  refund per customer; `adjustment.*` webhooks, migration 003).
- `POST /api/paddle/webhook` — Paddle only; `Paddle-Signature` verified on the
  raw body (5-minute window), idempotent via `public.paddle_events`.
- `DELETE /api/account` — same auth; JSON `{"confirm": "DELETE"}`. Deletes the
  profile row, then the Supabase auth user. `409 SUBSCRIPTION_ACTIVE` while a
  Paddle subscription still renews (active / trialing / past_due without a
  scheduled cancel); `400 CONFIRMATION_REQUIRED`; `429 RATE_LIMITED` (5 per
  user per 15 min, 20 per IP per hour, in memory per worker). Public help page:
  `delete-account.html` (en, fr, ar), the URL for Google Play.
- `POST /api/billing/portal` — same auth; returns Paddle customer portal links
  `{ overview_url, cancel_url, update_payment_url }` (503 without `PADDLE_API_KEY`).
- `POST /api/convert` — same auth; multipart field `file`. Machine-readable
  error codes: `AUTH_REQUIRED`, `INVALID_TOKEN`, `PROFILE_NOT_FOUND`,
  `FREE_LIMIT_REACHED` (HTTP 429 + `retry_after_seconds`), `DAILY_LIMIT_REACHED`
  (Pro fair use: `PRO_DAILY_LIMIT`, default 100 per rolling 24 h; HTTP 429 +
  `retry_after_seconds` + `limit`), `CONVERSION_FAILED`,
  `SERVER_ERROR`.

## Notes

- Max upload size: 25 MB, enforced on both frontend and backend; the backend
  also checks PDF magic bytes and re-opens the file before converting.
- Free quota (1 success / rolling 24h) is claimed atomically in Postgres
  (`claim_free_conversion`, row lock) and rolled back on conversion failure —
  simultaneous requests cannot double-spend, and failures never cost quota.
- Pro = `plan='pro'` **and** status `active` / `trialing` / `past_due`, or
  `canceled` before `current_period_end` (`billing.is_pro`; the same rule is in
  `claim_free_conversion`). Evaluated only from the database.
- Tests: `python -m unittest discover -s _tests`, `node _tests/billing_js_test.js`
  `node _tests/plan_ui_test.js`, `node _tests/account_delete_test.js` and
  `node _tests/auth_test.js`.
- Sign-in (`auth.js`, used by `login.html`, `fr/login.html`, `ar/login.html` and
  `forgot-password.html`): Google first (`prompt=select_account`); email sign up
  confirmed with the 6-digit code of the "Confirm signup" email (`verifyOtp`,
  type `signup`), resend after 60 s. Cloudflare Turnstile is prepared but OFF:
  set `TURNSTILE_SITE_KEY` at the top of `auth.js` and put the secret key in
  Supabase -> Authentication -> Attack Protection.
- Supabase Redirect URLs (Authentication -> URL Configuration): `https://pdf2wordly.com/`,
  `/fr/`, `/ar/` (Google returns to the page language) and `/reset-password.html`,
  `/fr/reset-password.html`, `/ar/reset-password.html` (password reset links).
  Passwords: at least 8 characters with letters and digits (checked in `auth.js`).
- Plan display: `plan-ui.js` is the only browser code that asks `/api/me`; it sets
  `<html data-plan-state>` (loading / signed-out / free / pro) and `style.css` shows
  "Get Pro" or the "Pro" badge. Pro users never see a purchase button.
- Converted files and uploads are temporary — deleted right after each
  response is sent (and on every failure path).
- `pdf2docx` handles standard text-based PDFs well. Complex layouts,
  scanned/image-only PDFs, and unusual fonts may convert imperfectly — that's
  a limitation of the library, not the integration.
