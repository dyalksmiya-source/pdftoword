// script.js conversion through /api/jobs: upload -> poll (3 s, 10 s after a minute, Retry-After,
// backoff on network errors) -> download (retried) -> DELETE. Runs the real script.js in a VM
// with a fake fetch and a fake clock.
// Run from website/:  node _tests/jobs_js_test.js
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const SITE = path.join(__dirname, '..');
const src = fs.readFileSync(path.join(SITE, 'script.js'), 'utf8');

let passed = 0;
const ok = (c, m) => { if (!c) throw new Error('FAIL: ' + m); passed++; console.log('ok -', m); };

function load(lang, routes) {
  const els = {};
  const el = id => (els[id] = els[id] || { id, textContent: '', style: {}, hidden: false,
    setAttribute(k, v) { this[k] = v; }, getAttribute(k) { return this[k]; } });
  const clock = { now: 1_000_000 };
  const sleeps = [];
  const calls = [];
  const document = {
    documentElement: { getAttribute: k => (k === 'lang' ? lang : null), setAttribute() {}, removeAttribute() {} },
    querySelector: () => null, querySelectorAll: () => [], getElementById: el, addEventListener() {},
  };
  const window = { location: { hostname: 'pdf2wordly.com', href: '' }, matchMedia: () => ({ matches: false }) };
  const fakeDate = { now: () => clock.now };
  const fetch = async (url, opt = {}) => {
    const method = opt.method || 'GET';
    calls.push(method + ' ' + url.replace('https://api.pdf2wordly.com', ''));
    const handler = routes(method, url.replace('https://api.pdf2wordly.com', ''), calls.length);
    if (handler instanceof Error) throw handler;
    const { status = 200, json, headers = {}, blob } = handler;
    return {
      status, ok: status >= 200 && status < 300,
      headers: { get: k => headers[k] || (json ? 'application/json' : null) && (k.toLowerCase() === 'content-type' ? 'application/json' : headers[k] || null) },
      json: async () => json, blob: async () => blob || 'BLOB',
    };
  };
  const ctx = {
    window, document, fetch, FormData: class { append() {} }, URL: { createObjectURL: b => 'blob:' + b, revokeObjectURL() {} },
    setTimeout: (f, ms) => { sleeps.push(ms); clock.now += ms; Promise.resolve().then(f); return 1; },
    clearTimeout() {}, Date: fakeDate, Promise, JSON, Math, Number, String, isFinite, encodeURIComponent, alert() {},
    localStorage: { getItem: () => null, setItem() {} }, console,
  };
  ctx.window.window = ctx.window;
  vm.runInNewContext(src, ctx);
  return { jobs: ctx.window.pwJobs, els, sleeps, calls, clock };
}

const JOB = 'A'.repeat(43);
const file = { name: 'Report.pdf' };

(async () => {
  // ---- 1. happy path: queued -> running -> done -> download -> delete
  {
    const seq = [{ status: 'queued', position: 3 }, { status: 'running', pages_done: 2, pages_total: 10 },
                 { status: 'done', expires_in_seconds: 3500 }];
    let i = 0;
    const t = load('en', (m, u) => {
      if (m === 'POST' && u === '/api/jobs') return { status: 202, json: { job_id: JOB, status: 'queued' } };
      if (m === 'GET' && u === `/api/jobs/${JOB}`) return { json: seq[i++] };
      if (m === 'GET' && u === `/api/jobs/${JOB}/download`) return { blob: 'DOCX' };
      if (m === 'DELETE') return { json: { deleted: true } };
      throw new Error('unexpected ' + m + ' ' + u);
    });
    const labels = [];
    const orig = t.els;
    const res = await t.jobs.pwJobConvert(file, 'tok');
    ok(res.fileName === 'Report.docx' && res.blobUrl === 'blob:DOCX', 'done: file name and blob');
    ok(t.calls.join('|') === `POST /api/jobs|GET /api/jobs/${JOB}|GET /api/jobs/${JOB}|GET /api/jobs/${JOB}|GET /api/jobs/${JOB}/download|DELETE /api/jobs/${JOB}`,
       'upload, 3 polls, download, then DELETE of the server copy: ' + t.calls.length + ' calls');
    ok(t.sleeps.slice(0, 3).every(ms => ms === 3000), 'polls every 3 s at first');
    ok(orig.progressFill && orig.progressFill.style.width === '97%', 'progress bar ends at 97% before the download');
    labels.push(t.jobs.pwJobLabel({ status: 'queued', position: 3 }), t.jobs.pwJobLabel({ status: 'running', pages_done: 2, pages_total: 10 }));
    ok(labels[0] === 'Waiting in line (2 ahead)...' && labels[1] === 'Converting page 3 of 10...', 'progress texts: queue place and page');
    ok(t.jobs.pwJobPct({ status: 'running', pages_done: 5, pages_total: 10 }) === 58, 'percent from pages done');
  }

  // ---- 2. poll timing: 10 s after the first minute, Retry-After on 429, capped
  {
    const t = load('en', () => ({}));
    ok(t.jobs.pwNextPoll(3000, 59_000, 0) === 3000 && t.jobs.pwNextPoll(3000, 60_000, 0) === 10_000, '3 s, then 10 s after a minute');
    ok(t.jobs.pwNextPoll(3000, 1000, 7) === 7000 && t.jobs.pwNextPoll(3000, 1000, 600) === 30_000, 'Retry-After respected, at most 30 s');
  }
  {
    let n = 0;
    const t = load('en', (m, u) => {
      if (m === 'POST') return { status: 202, json: { job_id: JOB } };
      if (u.endsWith('/download')) return { blob: 'D' };
      if (m === 'DELETE') return { json: {} };
      n++;
      if (n === 1) return { status: 429, json: { code: 'RATE_LIMITED' }, headers: { 'Retry-After': '7' } };
      if (n < 25) return { json: { status: 'running', pages_done: n, pages_total: 40 } };
      return { json: { status: 'done' } };
    });
    await t.jobs.pwJobConvert(file, 'tok');
    ok(t.sleeps[1] === 7000, 'after a 429 the next poll waits Retry-After (7 s)');
    ok(t.sleeps.includes(10_000) && Math.max(...t.sleeps) <= 30_000, 'long job: polls slow down to 10 s');
  }

  // ---- 3. network errors while polling: backoff, give up after 10
  {
    const t = load('en', (m) => (m === 'POST' ? { status: 202, json: { job_id: JOB } } : new TypeError('Failed to fetch')));
    let err;
    try { await t.jobs.pwJobConvert(file, 'tok'); } catch (e) { err = e; }
    ok(err && err.code === 'NETWORK_ERROR', 'network down: NETWORK_ERROR after 10 tries');
    ok(t.sleeps.slice(1, 5).join(',') === '6000,12000,24000,30000', 'backoff doubles, capped at 30 s: ' + t.sleeps.slice(0, 6));
  }

  // ---- 4. server answers: busy, failed job, expired job, download retried
  {
    const t = load('en', (m) => (m === 'POST' ? { status: 503, json: { code: 'SERVER_BUSY' } } : {}));
    let err;
    try { await t.jobs.pwJobConvert(file, 'tok'); } catch (e) { err = e; }
    ok(err.code === 'SERVER_BUSY', 'POST 503 -> SERVER_BUSY');
    ok(t.jobs.message('SERVER_BUSY') === 'The converter is busy right now. Please try again in a few minutes. This file was not counted toward your limit.',
       'busy message says it was not counted');
  }
  {
    const t = load('en', (m) => (m === 'POST' ? { status: 202, json: { job_id: JOB } }
      : { json: { status: 'failed', code: 'SCANNED_PAGE_LIMIT', scanned_pages: 60, max_scanned_pages: 50 } }));
    let err;
    try { await t.jobs.pwJobConvert(file, 'tok'); } catch (e) { err = e; }
    ok(err.code === 'SCANNED_PAGE_LIMIT' && err.scannedPages === 60 && err.maxScanned === 50, 'failed job carries the code and numbers');
    ok(t.jobs.message('SCANNED_PAGE_LIMIT', err).includes('up to 50 pages per file'), 'scanned message uses the server number');
  }
  {
    const t = load('en', (m) => (m === 'POST' ? { status: 202, json: { job_id: JOB } }
      : { json: { status: 'expired', code: 'JOB_EXPIRED', counted: false } }));
    let err;
    try { await t.jobs.pwJobConvert(file, 'tok'); } catch (e) { err = e; }
    ok(err.code === 'JOB_EXPIRED' && err.counted === false, 'expired job');
    ok(t.jobs.message('JOB_EXPIRED', err) === 'This file expired. Convert it again. This file was not counted toward your limit.',
       'expired, never downloaded: "convert it again, it does not count"');
    ok(t.jobs.message('JOB_EXPIRED', { counted: true }) === 'This file expired. Convert it again.', 'expired after a download: no "not counted"');
  }
  {
    let d = 0;
    const t = load('en', (m, u) => {
      if (m === 'POST') return { status: 202, json: { job_id: JOB } };
      if (u.endsWith('/download')) return ++d === 1 ? new TypeError('Failed to fetch') : { blob: 'OK' };
      if (m === 'DELETE') return { json: {} };
      return { json: { status: 'done' } };
    });
    const res = await t.jobs.pwJobConvert(file, 'tok');
    ok(res.blobUrl === 'blob:OK' && d === 2, 'download retried after a dropped connection');
  }
  {
    const t = load('en', (m, u) => {
      if (m === 'POST') return { status: 202, json: { job_id: JOB } };
      if (u.endsWith('/download')) return { status: 410, json: { code: 'JOB_EXPIRED', counted: false } };
      return { json: { status: 'done' } };
    });
    let err;
    try { await t.jobs.pwJobConvert(file, 'tok'); } catch (e) { err = e; }
    ok(err.code === 'JOB_EXPIRED' && !t.calls.some(c => c.startsWith('DELETE')), '410 on download -> JOB_EXPIRED, nothing deleted');
  }

  // ---- 5. texts in FR and AR for every new message (different from English, no English left)
  const en = load('en', () => ({}));
  for (const lang of ['fr', 'ar']) {
    const t = load(lang, () => ({}));
    for (const code of ['SERVER_BUSY', 'JOB_ACTIVE', 'JOB_EXPIRED', 'QUEUE_TIMEOUT', 'CONVERSION_INTERRUPTED']) {
      const m = t.jobs.message(code, {}), e = en.jobs.message(code, {});
      ok(m !== e && !e.split('. ').some(part => m.includes(part)), `${lang}: ${code} translated: ${m.slice(0, 45)}`);
    }
    for (const j of [{ status: 'running', pages_done: 0, pages_total: 4 }, { status: 'queued', position: 2 }, { status: 'queued', position: 1 }]) {
      ok(t.jobs.pwJobLabel(j) !== en.jobs.pwJobLabel(j), `${lang}: progress text translated: ${t.jobs.pwJobLabel(j)}`);
    }
  }
  ok(!/\/api\/convert/.test(src), 'script.js no longer calls /api/convert');

  console.log(`\n${passed} passed`);
})().catch(e => { console.error(e); process.exit(1); });
