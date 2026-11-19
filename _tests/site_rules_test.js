// Site texts for the anti-abuse rules + the conversion error messages of
// script.js (runs the real minified functions).
// Run from website/:  node _tests/site_rules_test.js
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const SITE = path.join(__dirname, '..');
const read = f => fs.readFileSync(path.join(SITE, f), 'utf8');

let passed = 0;
const ok = (c, m) => { if (!c) throw new Error('FAIL: ' + m); passed++; console.log('ok -', m); };

// ---- pull a function / the dictionary out of the minified script.js
const src = read('script.js');
function balanced(from) {
  const start = src.indexOf('{', from);
  let depth = 0, q = null, esc = false;
  for (let i = start; i < src.length; i++) {
    const ch = src[i];
    if (q) { if (esc) esc = false; else if (ch === '\\') esc = true; else if (ch === q) q = null; continue; }
    if (ch === '"' || ch === "'" || ch === '`') q = ch;
    else if (ch === '{') depth++;
    else if (ch === '}' && --depth === 0) return src.slice(from, i + 1);
  }
  throw new Error('unbalanced');
}
const DICT = JSON.parse(balanced(src.indexOf('{', src.indexOf('const pwI18n='))));
const fnY = balanced(src.indexOf('function y(e,p,x)'));
const fnDaily = balanced(src.indexOf('function pwDailyMsg(x)'));
function messages(lang) {
  const ctx = { pwT: k => (DICT[lang] && DICT[lang][k]) || k, Number, Math, String, isFinite };
  vm.runInNewContext(fnDaily + fnY + ';this.y=y;', ctx);
  return ctx.y;
}

// ---- 1. every error code gives a message (the old "returnpwT" bug threw)
ok(!/returnpwT/.test(src), 'script.js: no "returnpwT" (every message function returns)');
const y = messages('en');
for (const code of ['PASSWORD_REQUIRED', 'PAGE_LIMIT_EXCEEDED', 'AUTH_REQUIRED', 'INVALID_TOKEN', 'FREE_LIMIT_REACHED',
                    'SUBSCRIPTION_REQUIRED', 'CONVERSION_FAILED', 'NETWORK_ERROR', 'SERVER_ERROR', 'SOMETHING_NEW']) {
  const m = y(code, 20, {});
  ok(typeof m === 'string' && m.length > 10, `message for ${code}: "${m.slice(0, 50)}"`);
}
ok(y('PAGE_LIMIT_EXCEEDED', 20) === 'This PDF has too many pages. The limit is 20 pages per file.', 'page limit message with the number');
ok(messages('fr')('CONVERSION_FAILED') === 'Impossible de convertir ce fichier. Veuillez réessayer.', 'fr: conversion failed');

// ---- 2. Pro fair use message (DAILY_LIMIT_REACHED)
ok(y('DAILY_LIMIT_REACHED', undefined, { limit: 100, retryAfter: 3 * 3600 + 20 * 60 }) ===
   "You've reached the fair-use limit of 100 files in 24 hours. Try again in 3 h 20 min.", 'en: fair-use limit + time');
ok(y('DAILY_LIMIT_REACHED', undefined, { limit: 100 }) === "You've reached the fair-use limit of 100 files in 24 hours.", 'en: without retry time');
ok(messages('fr')('DAILY_LIMIT_REACHED', undefined, { limit: 100, retryAfter: 90 }) ===
   'Vous avez atteint la limite d’usage raisonnable de 100 fichiers sur 24 heures. Réessayez dans 0 h 2 min.', 'fr: fair-use limit');
ok(messages('ar')('DAILY_LIMIT_REACHED', undefined, { limit: 100, retryAfter: 3600 }) ===
   'لقد بلغت حد الاستخدام العادل البالغ 100 ملف خلال 24 ساعة. حاول مرة أخرى بعد 1 س 0 د.', 'ar: fair-use limit');
ok(/L=e\.limit/.test(src) && /limit:L\}/.test(src) && /y\(t,e&&e\.maxPages,e\)/.test(src), 'script.js passes limit and retry time from the 429 answer');

// ---- 3. one number in one place: site-config "pro-daily-limit"
const cfg = JSON.parse(read('_tools/site-config.json'));
const N = cfg['pro-daily-limit'];
ok(N === '100', 'site-config pro-daily-limit = 100');
ok(new RegExp(`PRO_DAILY_LIMIT = int\\(os.environ.get\\("PRO_DAILY_LIMIT", "${N}"\\)\\)`).test(read('server.py')), 'server default limit = site-config number');
const span = `<span data-config="pro-daily-limit">${N}</span>`;
for (const [f, li, faq, ld] of [
  ['index.html', `<li>Up to ${span} files per day</li>`, `Pro allows up to ${span} files per day.</p>`, `Pro allows up to ${N} files per day."`],
  ['fr/index.html', `<li>Jusqu’à ${span} fichiers par jour</li>`, `La formule Pro permet jusqu’à ${span} fichiers par jour.</p>`, `La formule Pro permet jusqu’à ${N} fichiers par jour."`],
  ['ar/index.html', `<li>حتى ${span} ملف يوميًا</li>`, `وباقة Pro تتيح حتى ${span} ملف يوميًا.</p>`, `وباقة Pro تتيح حتى ${N} ملف يوميًا."`]]) {
  const s = read(f);
  const card = s.slice(s.indexOf('price-card-featured'), s.indexOf('id="proCta"'));
  ok(card.includes(li) && (card.match(/<li>/g) || []).length === 4, `${f}: pricing card "${li.replace(/<[^>]+>/g, '')}" (4 items, no separate fair-use line)`);
  ok(s.includes(faq), `${f}: FAQ answer`);
  ok(s.includes(ld), `${f}: FAQ structured data says the same number (${N})`);
}

// ---- no page promises unlimited use
const pages = fs.readdirSync(SITE).filter(x => x.endsWith('.html') || x === 'llms.txt')
  .concat(fs.readdirSync(path.join(SITE, 'fr')).map(x => 'fr/' + x), fs.readdirSync(path.join(SITE, 'ar')).map(x => 'ar/' + x))
  .filter(x => /\.(html|txt)$/.test(x));
const UNLIMITED = /unlimited|illimit|sans limite|غير محدود|بلا حدود|removes? (that|the) (daily )?(conversion )?limit|lève les limites|supprime cette limite|تزيل هذا الحد|يزيل الحدود|never stall/i;
const offenders = pages.filter(x => UNLIMITED.test(read(x)));
ok(pages.length >= 30 && offenders.length === 0, `no page promises unlimited use (${pages.length} files checked)` + (offenders.length ? ': ' + offenders.join(', ') : ''));
// every number written in structured data / llms.txt is the config number
const lits = [];
for (const x of pages) {
  for (const m of read(x).matchAll(/up to (\d+) files per day|jusqu’à (\d+) fichiers par jour|حتى (\d+) ملف يوميًا/g)) lits.push([x, m[1] || m[2] || m[3]]);
}
ok(lits.length >= 8 && lits.every(([, n]) => n === N), `${lits.length} plain "${N} files per day" mentions (structured data, llms.txt) match site-config`);
// page text uses the config span everywhere else
const spans = pages.reduce((n, x) => n + (read(x).match(/data-config="pro-daily-limit"/g) || []).length, 0);
ok(spans >= 15, `${spans} page mentions come from site-config (data-config="pro-daily-limit")`);
const terms = read('terms.html');
ok(terms.includes(`<li>Up to ${span} files per day (rolling 24 hours)</li>`), 'terms: up to 100 files per day');
ok(terms.includes('After a refund, the account cannot subscribe to Pro again.'), 'terms: no Pro again after a refund');
ok(terms.includes('per month (USD), taxes included.') && !terms.includes('plus any tax'), 'terms: price includes tax (as in Paddle)');
ok(!terms.includes('available when Pro launches'), 'terms: no "available when Pro launches"');
const refund = read('refund-policy.html');
ok(refund.includes('After a refund, the account cannot subscribe to Pro again.'), 'refund policy: same rule');
const refundBody = refund.slice(refund.indexOf('<main'), refund.indexOf('</main>')).replace(/<[^>]+>/g, ' ');
ok(!/\b(unless|only if|provided that|eligible|except|must|subject to)\b/i.test(refundBody), 'refund policy: still no conditional wording');
ok(fs.readFileSync(path.join(SITE, 'billing.js'), 'utf8').includes("can_purchase === false"), 'billing.js checks can_purchase');

console.log(`\n${passed} passed`);
