// Tests for plan-ui.js, the first-paint <head> snippet and the Pro markup.
// Run from website/:  node _tests/plan_ui_test.js
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const SITE = path.join(__dirname, '..');
const P = require(path.join(SITE, 'plan-ui.js'));

let passed = 0;
const ok = (c, m) => { if (!c) throw new Error('FAIL: ' + m); passed++; console.log('ok -', m); };

// ---- the real pwI18n dictionary from script.js (same i18n system)
const scriptSrc = fs.readFileSync(path.join(SITE, 'script.js'), 'utf8');
function extractDict(src) {
  const start = src.indexOf('{', src.indexOf('const pwI18n='));
  let depth = 0, inStr = false, esc = false;
  for (let i = start; i < src.length; i++) {
    const ch = src[i];
    if (inStr) { if (esc) esc = false; else if (ch === '\\') esc = true; else if (ch === '"') inStr = false; continue; }
    if (ch === '"') inStr = true;
    else if (ch === '{') depth++;
    else if (ch === '}' && --depth === 0) return JSON.parse(src.slice(start, i + 1));
  }
  throw new Error('pwI18n not found');
}
const DICT = extractDict(scriptSrc);
const pwTFor = l => k => (DICT[l] && DICT[l][k]) || k;
ok(DICT.fr['Pro until {date}'] === 'Pro jusqu’au {date}' && DICT.ar['Pro until {date}'] === 'Pro حتى {date}', 'i18n: "Pro until {date}" in fr and ar (pwI18n)');
ok(DICT.fr.Free === 'Gratuit' && !!DICT.ar.Free, 'i18n: "Free" in fr and ar');
ok(/window\.pwT=pwT;/.test(scriptSrc), 'script.js exposes pwT for plan-ui.js');
ok(!/\/api\/me/.test(scriptSrc) && /window\.pwPlan\.refresh\(\)/.test(scriptSrc), 'script.js no longer fetches /api/me itself: it uses pwPlan.refresh()');

// ---- tiny DOM
function makeDoc(lang = 'en', state) {
  const attrs = { lang };
  if (state) attrs['data-plan-state'] = state;
  const badge = { textContent: '', hidden: true, attrs: { hidden: '' },
    setAttribute(k, v) { this.attrs[k] = String(v); }, removeAttribute(k) { delete this.attrs[k]; }, getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; } };
  return {
    badge,
    documentElement: { attrs, getAttribute: k => (k in attrs ? attrs[k] : null), setAttribute: (k, v) => { attrs[k] = String(v); } },
    querySelector: () => null,
    querySelectorAll: sel => (sel === '[data-plan-badge]' ? [badge] : []),
  };
}
function storage(init = {}) {
  const m = { ...init };
  return { m, getItem: k => (k in m ? m[k] : null), setItem: (k, v) => { m[k] = String(v); }, removeItem: k => { delete m[k]; } };
}
function makeWin({ lang = 'en', session = null, store = storage() } = {}) {
  const listeners = [];
  const w = {
    location: { hostname: 'pdf2wordly.com' },
    sessionStorage: store,
    pwT: pwTFor(lang),
    __supabaseClient: { auth: {
      getSession: async () => ({ data: { session: w._session } }),
      onAuthStateChange: fn => listeners.push(fn),
    } },
    _session: session,
    _fire: ev => listeners.forEach(fn => fn(ev)),
  };
  return w;
}
const USER = { id: 'u-1', email: 'eve@example.com' };
const sess = (u = USER) => ({ user: u, access_token: 'tok-' + u.id });
function fetchMe(body, opts = {}) {
  const f = async (url, o) => {
    f.calls++; f.last = { url, o };
    if (opts.delay) await new Promise(r => setTimeout(r, opts.delay));
    if (opts.fail) throw new Error('network');
    if (opts.status) return { ok: false, status: opts.status };
    return { ok: true, json: async () => body };
  };
  f.calls = 0;
  return f;
}
const state = doc => doc.documentElement.getAttribute('data-plan-state');

(async () => {
  // signed out
  let doc = makeDoc('en', 'loading'); let w = makeWin({ store: storage({ 'pw-plan': JSON.stringify({ u: 'old', p: 'pro' }) }) });
  let plan = P.create(doc, w, fetchMe({}));
  let r = await plan.refresh();
  ok(state(doc) === 'signed-out' && r.plan === null && doc.badge.hidden, 'signed out: state signed-out, badge hidden');
  ok(!('pw-plan' in w.sessionStorage.m), 'signed out: cached plan cleared');

  // free
  doc = makeDoc(); w = makeWin({ session: sess() });
  let f = fetchMe({ plan: 'free', subscription_status: null });
  plan = P.create(doc, w, f);
  r = await plan.refresh();
  ok(state(doc) === 'free' && r.plan === 'free' && plan.state === 'free', 'free: state free');
  ok(doc.badge.textContent === 'Free' && !doc.badge.hidden && doc.badge.getAttribute('data-plan') === 'free', 'free: menu badge "Free"');
  ok(f.last.url === 'https://api.pdf2wordly.com/api/me' && f.last.o.headers.Authorization === 'Bearer tok-u-1', 'calls /api/me with the session token');
  ok(JSON.parse(w.sessionStorage.m['pw-plan']).p === 'free' && JSON.parse(w.sessionStorage.m['pw-plan']).u === 'u-1', 'free: cached per user id');

  // pro
  doc = makeDoc(); w = makeWin({ session: sess() });
  plan = P.create(doc, w, fetchMe({ plan: 'pro', subscription_status: 'active', renews_at: '2026-12-18T00:00:00Z', cancel_scheduled: false }));
  await plan.refresh();
  ok(state(doc) === 'pro' && doc.badge.textContent === 'Pro' && doc.badge.getAttribute('data-plan') === 'pro', 'pro: state pro, menu badge "Pro"');
  ok(JSON.parse(w.sessionStorage.m['pw-plan']).p === 'pro', 'pro: cached');

  // pro with a scheduled cancellation, 3 languages
  const cancel = { plan: 'pro', subscription_status: 'active', cancel_scheduled: true, ends_at: '2026-12-18T12:00:00Z' };
  for (const [lang, expected] of [['en', 'Pro until December 18, 2026'], ['fr', 'Pro jusqu’au 18 décembre 2026'], ['ar', 'Pro حتى ']]) {
    doc = makeDoc(lang); w = makeWin({ lang, session: sess() });
    plan = P.create(doc, w, fetchMe(cancel));
    await plan.refresh();
    ok(lang === 'ar' ? doc.badge.textContent.startsWith(expected) && doc.badge.textContent.length > expected.length + 4 : doc.badge.textContent === expected,
       `cancel scheduled (${lang}): "${doc.badge.textContent}"`);
  }

  // /api/me wins over the cache
  doc = makeDoc('en', 'pro'); w = makeWin({ session: sess(), store: storage({ 'pw-plan': JSON.stringify({ u: 'u-1', p: 'pro' }) }) });
  plan = P.create(doc, w, fetchMe({ plan: 'free' }));
  await plan.refresh();
  ok(state(doc) === 'free' && JSON.parse(w.sessionStorage.m['pw-plan']).p === 'free', '/api/me wins: cached Pro, server Free -> Free');

  // /api/me fails: keep this user's cached plan, never another user's
  doc = makeDoc('en', 'pro'); w = makeWin({ session: sess(), store: storage({ 'pw-plan': JSON.stringify({ u: 'u-1', p: 'pro' }) }) });
  plan = P.create(doc, w, fetchMe(null, { status: 502 }));
  await plan.refresh();
  ok(state(doc) === 'pro', '/api/me fails: same user cached Pro kept');
  doc = makeDoc('en', 'loading'); w = makeWin({ session: sess(), store: storage({ 'pw-plan': JSON.stringify({ u: 'someone-else', p: 'pro' }) }) });
  plan = P.create(doc, w, fetchMe(null, { fail: true }));
  await plan.refresh();
  ok(state(doc) === 'free', "/api/me fails: another user's cache ignored -> Free");

  // concurrent refreshes share one request
  doc = makeDoc(); w = makeWin({ session: sess() });
  f = fetchMe({ plan: 'pro' }, { delay: 5 });
  plan = P.create(doc, w, f);
  const [a, b] = await Promise.all([plan.refresh(), plan.refresh()]);
  ok(f.calls === 1 && a.plan === 'pro' && b.plan === 'pro', 'two refreshes at once -> one /api/me request');
  await plan.refresh();
  ok(f.calls === 2, 'a later refresh asks again');

  // auth changes
  doc = makeDoc(); w = makeWin({ session: sess() });
  f = fetchMe({ plan: 'pro' });
  plan = P.create(doc, w, f);
  await plan.refresh();
  w._session = null; w._fire('SIGNED_OUT');
  await new Promise(r => setTimeout(r, 5));
  ok(state(doc) === 'signed-out' && !('pw-plan' in w.sessionStorage.m), 'sign-out -> Get Pro again, cache cleared');
  const before = f.calls; w._fire('TOKEN_REFRESHED'); await new Promise(r => setTimeout(r, 5));
  ok(f.calls === before, 'token refresh does not re-ask /api/me');

  // update(me) without a request (used by the checkout guard / after payment)
  doc = makeDoc(); w = makeWin({ session: sess() });
  f = fetchMe({});
  plan = P.create(doc, w, f);
  let seen = null; plan.onChange((s, me) => { seen = s; });
  plan.update({ plan: 'pro' }, 'u-1');
  ok(state(doc) === 'pro' && seen === 'pro' && f.calls === 0 && plan.me.plan === 'pro', 'update(me): applied immediately, listeners told, no request');

  // storage blocked
  const blocked = { getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); }, removeItem() { throw new Error('blocked'); } };
  doc = makeDoc(); w = makeWin({ session: sess(), store: blocked });
  plan = P.create(doc, w, fetchMe({ plan: 'pro' }));
  await plan.refresh();
  ok(state(doc) === 'pro', 'sessionStorage blocked: still works');

  // ---- first-paint <head> snippet (run the real one from index.html)
  const html = fs.readFileSync(path.join(SITE, 'index.html'), 'utf8');
  const snippet = html.match(/<script>(try\{var u=\(JSON\.parse\(localStorage\.getItem\('sb-yjxfxdbrjsiuevnkiiak-auth-token'\).*?)<\/script>/)[1];
  function runHead(local, session) {
    const attrs = {};
    const ctx = {
      JSON,
      document: { documentElement: { setAttribute: (k, v) => { attrs[k] = v; } } },
      localStorage: local === 'throw' ? { getItem() { throw new Error('x'); } } : { getItem: k => (k in local ? local[k] : null) },
      sessionStorage: session === 'throw' ? { getItem() { throw new Error('x'); } } : { getItem: k => (k in session ? session[k] : null) },
    };
    vm.runInNewContext(snippet, ctx);
    return attrs['data-plan-state'];
  }
  const sb = { 'sb-yjxfxdbrjsiuevnkiiak-auth-token': JSON.stringify({ access_token: 't', user: { id: 'u-1' } }) };
  ok(runHead({}, {}) === 'signed-out', 'head: no Supabase session -> signed-out (Get Pro at once)');
  ok(runHead(sb, {}) === 'loading', 'head: session, no cache -> loading placeholder');
  ok(runHead(sb, { 'pw-plan': JSON.stringify({ u: 'u-1', p: 'pro' }) }) === 'pro', 'head: cached Pro for this user -> Pro badge at once');
  ok(runHead(sb, { 'pw-plan': JSON.stringify({ u: 'u-1', p: 'free' }) }) === 'free', 'head: cached Free for this user -> Get Pro at once');
  ok(runHead(sb, { 'pw-plan': JSON.stringify({ u: 'u-2', p: 'pro' }) }) === 'loading', "head: another user's cache ignored");
  ok(runHead(sb, { 'pw-plan': '{bad' }) === 'loading', 'head: broken cache -> loading');
  ok(runHead(sb, 'throw') === 'loading', 'head: sessionStorage blocked -> loading');
  ok(runHead('throw', {}) === 'loading', 'head: localStorage blocked -> loading');

  // ---- every header page has the markup, in the right order
  const pages = fs.readdirSync(SITE).filter(f => f.endsWith('.html')).map(f => f)
    .concat(['fr/index.html', 'ar/index.html', 'fr/delete-account.html', 'ar/delete-account.html'])
    .filter(f => fs.readFileSync(path.join(SITE, f), 'utf8').includes('header-get-pro'));
  ok(pages.length === 24, 'pages with a header: 24');
  for (const pg of pages) {
    const s = fs.readFileSync(path.join(SITE, pg), 'utf8');
    const sub = pg.includes('/');
    const pre = sub ? '../' : '';
    const head = s.slice(0, s.indexOf('</head>'));
    const badges = (s.match(/data-pro-badge/g) || []).length;
    const mobiles = (s.match(/id="getProBtnMobile"/g) || []).length;
    ok(head.includes("setAttribute('data-plan-state',s)") && head.indexOf('pdftoword-theme') < head.indexOf('data-plan-state')
       && badges === 1 + mobiles
       && (s.match(new RegExp(`<a href="${pre}account\\.html" class="btn btn-secondary header-pro-badge`, 'g')) || []).length === badges
       && s.indexOf(`<script src="${pre}plan-ui.js" defer></script>`) !== -1
       && s.indexOf(`<script src="${pre}plan-ui.js" defer></script>`) < s.indexOf(`<script src="${pre}script.js" defer></script>`)
       && !/href="[^"]*#pricing"[^>]*data-pro-badge/.test(s),
       `${pg}: head state, ${badges} Pro badge(s) -> account.html, plan-ui.js before script.js`);
  }
  for (const [pg, cur, manage, label] of [
    ['index.html', 'Your current plan', 'Manage subscription', 'Pro plan – account settings'],
    ['fr/index.html', 'Votre formule actuelle', 'Gérer l’abonnement', 'Formule Pro – paramètres du compte'],
    ['ar/index.html', 'خطتك الحالية', 'إدارة الاشتراك', 'خطة Pro – إعدادات الحساب']]) {
    const s = fs.readFileSync(path.join(SITE, pg), 'utf8');
    const acct = pg.includes('/') ? '../account.html' : 'account.html';
    const card = s.slice(s.indexOf('id="proCta"'), s.indexOf('</div>', s.indexOf('data-pro-current')) + 6);
    ok(card.includes(`aria-disabled="true">${cur}</span>`) && card.includes(`<a href="${acct}" class="pro-manage-link">${manage}</a>`), `${pg}: Pro card "${cur}" + "${manage}"`);
    ok(s.includes(`aria-label="${label}"`), `${pg}: badge label "${label}"`);
    const free = s.slice(s.indexOf('<div class="price-card">'), s.indexOf('<div class="price-card price-card-featured">'));
    ok(!/current|actuelle|الحالية/i.test(free), `${pg}: Free card has no "current plan"`);
  }

  // ---- CSS rules
  const css = fs.readFileSync(path.join(SITE, 'style.css'), 'utf8');
  for (const rule of [
    'html[data-plan-state=pro] .header-pro-badge{display:inline-flex}',
    'html[data-plan-state=pro] .header-get-pro,html[data-plan-state=pro] #getProBtnMobile{display:none!important}',
    'html[data-plan-state=loading] .header-get-pro,html[data-plan-state=loading] #getProBtnMobile{visibility:hidden;pointer-events:none}',
    'html[data-plan-state=pro] .pro-current{display:block}',
    'html[data-plan-state=pro] #proCta{display:none!important}',
    'html[data-plan-state=loading] #proCta{visibility:hidden;pointer-events:none}',
    '.header-pro-badge{display:none;',
    '.pro-current{display:none}',
  ]) ok(css.includes(rule), 'css: ' + rule.slice(0, 70));
  ok(css.lastIndexOf('.header-pro-badge{display:none;') > css.lastIndexOf('.btn{display:inline-flex'), 'css: badge rule after .btn (wins the cascade)');

  console.log(`\n${passed} passed`);
})().catch(e => { console.error(e.message); process.exit(1); });
