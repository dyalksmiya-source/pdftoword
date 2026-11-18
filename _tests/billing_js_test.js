// Tests for billing.js and the subscription buttons in account.js.
// Run from website/:  node _tests/billing_js_test.js
'use strict';
const path = require('path');
const B = require(path.join(__dirname, '..', 'billing.js'));
const A = require(path.join(__dirname, '..', 'account.js'));

let passed = 0;
const ok = (c, m) => { if (!c) throw new Error('FAIL: ' + m); passed++; console.log('ok -', m); };

// ---- tiny DOM ---------------------------------------------------------------
function el(tag, attrs = {}) {
  const e = {
    tagName: tag.toUpperCase(), attrs: { ...attrs }, children: [], parentNode: null, listeners: {},
    textContent: attrs.text || '', hidden: false, disabled: false, className: '',
    setAttribute(k, v) { this.attrs[k] = String(v); if (k === 'id') this.id = String(v); },
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    removeAttribute(k) { delete this.attrs[k]; },
    addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); },
    appendChild(c) { c.parentNode = this; this.children.push(c); if (this.onAppend) this.onAppend(c); return c; },
    insertBefore(c, ref) { c.parentNode = this; const i = ref ? this.children.indexOf(ref) : -1; i < 0 ? this.children.push(c) : this.children.splice(i, 0, c); return c; },
    replaceChild(n, o) { const i = this.children.indexOf(o); this.children[i] = n; n.parentNode = this; o.parentNode = null; return o; },
    get nextSibling() { const s = this.parentNode.children; return s[s.indexOf(this) + 1] || null; },
    click() { return Promise.all((this.listeners.click || []).map(f => f())); },
  };
  if (attrs.id) e.id = attrs.id;
  return e;
}
function homePage(lang = 'en') {
  const card = el('div');
  const label = { en: 'Pro is coming soon', fr: 'Pro arrive bientôt', ar: 'Pro قريبًا' }[lang];
  const span = el('span', { id: 'proCta', text: label });
  card.appendChild(span);
  const price = el('span', { text: '$2.99' });
  const badge = el('span', { 'data-plan-badge': '' }); badge.hidden = true;
  const head = el('head');
  const doc = {
    documentElement: { lang }, head, card,
    getElementById: id => (function find(n) { if (n.id === id) return n; for (const c of n.children) { const r = find(c); if (r) return r; } return null; })(card),
    querySelector: sel => (sel === '[data-config="pro-price"]' ? price : null),
    querySelectorAll: sel => (sel === '[data-plan-badge]' ? [badge] : []),
    createElement: tag => el(tag),
    badge,
  };
  return doc;
}
function storageMock(init = {}) {
  const m = { ...init };
  return { getItem: k => (k in m ? m[k] : null), setItem: (k, v) => { m[k] = String(v); }, removeItem: k => { delete m[k]; }, m };
}
function windowMock({ path = '/', search = '', hash = '', store = storageMock(), paddle } = {}) {
  const w = {
    location: { pathname: path, search, hash, hostname: 'pdf2wordly.com', href: null, replaced: null, replace(u) { this.replaced = u; } },
    sessionStorage: store,
  };
  if (paddle) w.Paddle = paddle;
  return w;
}
function paddleMock() {
  const p = { env: null, init: null, opened: null };
  p.Environment = { set: e => { p.env = e; } };
  p.Initialize = o => { p.init = o; };
  p.Checkout = { open: o => { p.opened = o; } };
  return p;
}
const user = { id: '3f1c2a7e-6b1d-4c55-9a8e-2d3b4c5d6e7f', email: 'eve@example.com' };
const client = s => ({ auth: { getSession: async () => ({ data: { session: s } }) } });
const signedIn = client({ user, access_token: 'tok' });
const signedOut = client(null);

(async () => {
  // ---- config in one place
  ok(B.PADDLE.env === 'sandbox' && B.PADDLE.priceId === 'pri_01m7qw0f7qr17f82dtkdx560wv'
     && B.PADDLE.clientToken === 'test_7b94c0b97260a1251ddd3166350'
     && B.PADDLE.scriptUrl === 'https://cdn.paddle.com/paddle/v2/paddle.js', 'config: env, token, price, Paddle.js v2 URL');

  // ---- hidden without the flag
  let doc = homePage(); let w = windowMock();
  ok(await B.init(doc, w, signedIn, async () => ({})) === 'hidden', 'no flag: hidden');
  ok(doc.head.children.length === 0, 'no flag: Paddle.js not loaded');
  ok(doc.getElementById('proCta').tagName === 'SPAN' && doc.getElementById('proCta').textContent === 'Pro is coming soon', 'no flag: button unchanged');
  w = windowMock({ search: '?paddle=live' });
  ok(await B.init(homePage(), w, signedIn, async () => ({})) === 'hidden', 'other flag values: hidden');

  // ---- flag remembered in sessionStorage, ?paddle=off forgets it
  const store = storageMock();
  ok(B.flagEnabled(windowMock({ search: '?paddle=sandbox', store })) && store.m['pw-paddle'] === 'sandbox', 'flag: ?paddle=sandbox stored');
  ok(B.flagEnabled(windowMock({ store })), 'flag: remembered on the next page');
  ok(!B.flagEnabled(windowMock({ search: '?paddle=off', store })) && !('pw-paddle' in store.m), 'flag: ?paddle=off forgets it');
  const throwing = { location: { search: '?paddle=sandbox' }, get sessionStorage() { throw new Error('blocked'); } };
  ok(B.flagEnabled(throwing) === false, 'flag: blocked storage -> hidden, no crash');

  // ---- with the flag: label in en, fr, ar; Paddle initialised
  for (const [lang, label] of [['en', 'Subscribe – $2.99/month'], ['fr', 'S’abonner – $2.99/mois'], ['ar', 'اشترك – $2.99 شهريًا']]) {
    const p = paddleMock();
    doc = homePage(lang);
    w = windowMock({ path: lang === 'en' ? '/' : `/${lang}/`, search: '?paddle=sandbox', paddle: p });
    ok(await B.init(doc, w, signedIn, async () => ({})) === 'ready', `${lang}: ready`);
    const btn = doc.getElementById('proCta');
    ok(btn.tagName === 'BUTTON' && btn.textContent === label, `${lang}: label "${label}"`);
    ok(p.env === 'sandbox' && p.init.token === B.PADDLE.clientToken && typeof p.init.eventCallback === 'function', `${lang}: Paddle sandbox + token`);
    await btn.click();
    ok(p.opened && p.opened.items.length === 1 && p.opened.items[0].priceId === B.PADDLE.priceId && p.opened.items[0].quantity === 1, `${lang}: checkout items`);
    ok(p.opened.customer.email === user.email && p.opened.customData.supabase_user_id === user.id, `${lang}: email + supabase_user_id`);
    ok(p.opened.settings.displayMode === 'overlay' && p.opened.settings.locale === lang, `${lang}: overlay, locale ${lang}`);
  }

  // ---- Paddle.js loaded from the CDN when not present
  doc = homePage(); w = windowMock({ search: '?paddle=sandbox' });
  doc.head.onAppend = s => { w.Paddle = paddleMock(); s.onload(); };
  ok(await B.init(doc, w, signedIn, async () => ({})) === 'ready' && doc.head.children[0].src === B.PADDLE.scriptUrl, 'loads Paddle.js v2 from the CDN');
  doc = homePage(); w = windowMock({ search: '?paddle=sandbox' });
  doc.head.onAppend = s => s.onerror();
  ok(await B.init(doc, w, signedIn, async () => ({})) === 'error' && doc.getElementById('proCta').tagName === 'SPAN', 'Paddle.js fails: button stays "coming soon"');

  // ---- signed out: remember #pricing and go to login
  for (const [p, login, back] of [['/', 'login.html', '/#pricing'], ['/fr/', '../login.html', '/fr/#pricing'], ['/ar/', '../login.html', '/ar/#pricing']]) {
    const st = storageMock({ 'pw-paddle': 'sandbox' });
    const pm = paddleMock();
    doc = homePage(); w = windowMock({ path: p, store: st, paddle: pm });
    await B.init(doc, w, signedOut, async () => ({}));
    await doc.getElementById('proCta').click();
    ok(w.location.href === login && JSON.parse(st.m['pw-return']).p === back && !pm.opened, `signed out on ${p}: login, return to ${back}`);
  }

  // ---- back after login
  const t0 = 1_000_000;
  let st = storageMock({ 'pw-return': JSON.stringify({ p: '/fr/#pricing', t: t0 }) });
  w = windowMock({ path: '/', store: st });
  ok(await B.init(homePage(), w, signedIn, async () => ({}), t0 + 60_000) === 'redirect' && w.location.replaced === '/fr/#pricing', 'after login: back to /fr/#pricing');
  ok(!('pw-return' in st.m), 'after login: return target used once');
  st = storageMock({ 'pw-return': JSON.stringify({ p: '/fr/#pricing', t: t0 }) });
  w = windowMock({ store: st });
  await B.init(homePage(), w, signedOut, async () => ({}), t0);
  ok(w.location.replaced === null && 'pw-return' in st.m, 'still signed out: no redirect, target kept');
  for (const bad of ['https://evil.example/#pricing', '//evil.example', '/login.html', 'javascript:alert(1)', '/fr/#pricing?x']) {
    st = storageMock({ 'pw-return': JSON.stringify({ p: bad, t: t0 }) });
    w = windowMock({ store: st });
    await B.init(homePage(), w, signedIn, async () => ({}), t0);
    ok(w.location.replaced === null, 'unsafe return target refused: ' + bad);
  }
  st = storageMock({ 'pw-return': JSON.stringify({ p: '/#pricing', t: t0 }) });
  w = windowMock({ store: st });
  await B.init(homePage(), w, signedIn, async () => ({}), t0 + 31 * 60_000);
  ok(w.location.replaced === null, 'return target older than 30 min ignored');
  st = storageMock({ 'pw-return': 'not json' }); w = windowMock({ store: st });
  ok(await B.init(homePage(), w, signedIn, async () => ({}), t0) === 'hidden', 'broken return value ignored');

  // ---- polling /api/me
  let clock = 0; const sleep = async ms => { clock += ms; };
  let calls = 0;
  const fetchPro = after => async () => { calls++; return { ok: true, json: async () => ({ plan: calls > after ? 'pro' : 'free' }) }; };
  calls = 0;
  ok(await B.pollPro(fetchPro(2), 'u', async () => 'tok', { now: () => clock, sleep }) === 'pro' && calls === 3, 'poll: stops when plan = pro');
  clock = 0; calls = 0;
  ok(await B.pollPro(fetchPro(999), 'u', async () => 'tok', { now: () => clock, sleep }) === 'timeout' && clock === 60000 && calls === 21, 'poll: gives up at 60 s (21 calls, 3 s apart)');
  clock = 0; calls = 0;
  const flaky = async () => { calls++; if (calls < 3) throw new Error('net'); return { ok: true, json: async () => ({ plan: 'pro' }) }; };
  ok(await B.pollPro(flaky, 'u', async () => 'tok', { now: () => clock, sleep }) === 'pro', 'poll: network errors retried');

  // ---- checkout.completed -> message, then badge Pro
  const p2 = paddleMock(); doc = homePage();
  w = windowMock({ search: '?paddle=sandbox', paddle: p2 });
  await B.init(doc, w, signedIn, async () => ({ ok: true, json: async () => ({ plan: 'pro' }) }));
  const status = doc.card.children[1];
  const done = p2.init.eventCallback({ name: 'checkout.completed' });
  ok(status.textContent === 'Payment received, activating Pro…' && !status.hidden, 'completed: "Payment received, activating Pro…"');
  await done;
  ok(doc.badge.textContent === 'Pro' && doc.badge.getAttribute('data-plan') === 'pro' && !doc.badge.hidden, 'completed: badge refreshed to Pro');
  ok(status.textContent === 'Pro is active. Thank you!', 'completed: confirmation shown');
  await p2.init.eventCallback({ name: 'checkout.loaded' });
  ok(status.textContent === 'Pro is active. Thank you!', 'other Paddle events ignored');

  // ---- account.js
  const d = A.describe;
  ok(d({ plan: 'pro', subscription_status: 'active', renews_at: '2026-12-18T00:00:00Z' }).status === 'Active, renews on December 18, 2026', 'account: renews on');
  ok(d({ plan: 'pro', subscription_status: 'active', cancel_scheduled: true, ends_at: '2026-12-18T00:00:00Z' }).status === 'Cancellation scheduled: Pro until December 18, 2026', 'account: cancellation scheduled');
  ok(d({ plan: 'pro', subscription_status: 'canceled', ends_at: '2026-12-18T00:00:00Z' }).status === 'Cancelled: Pro until December 18, 2026', 'account: cancelled, Pro until');
  ok(d({ plan: 'pro', subscription_status: 'past_due' }).plan === 'Pro', 'account: past_due still Pro');

  function accountDoc() {
    const ids = {};
    ['billingActions', 'billingError', 'manageSubscription', 'cancelSubscription'].forEach(id => { ids[id] = el('div', { id }); });
    ids.billingActions.hidden = true; ids.billingError.hidden = true;
    return { ids, getElementById: id => ids[id] || null };
  }
  let ad = accountDoc(); let aw = { location: { href: null } };
  ok(A.setupBilling(ad, aw, { plan: 'free' }, 'https://api', 'tok', null) === false && ad.ids.billingActions.hidden, 'account: buttons hidden for Free');
  ok(A.setupBilling(ad, aw, { plan: 'pro', billing_source: null }, 'https://api', 'tok', null) === false, 'account: hidden for Pro without Paddle');
  let last = null;
  const portalFetch = async (url, opts) => { last = { url, opts }; return { ok: true, json: async () => ({ overview_url: 'https://customer-portal.paddle.com/o', cancel_url: 'https://customer-portal.paddle.com/c' }) }; };
  ad = accountDoc(); aw = { location: { href: null } };
  ok(A.setupBilling(ad, aw, { plan: 'pro', billing_source: 'paddle' }, 'https://api', 'tok', portalFetch) && !ad.ids.billingActions.hidden, 'account: buttons shown for Paddle Pro');
  await ad.ids.manageSubscription.click();
  ok(last.url === 'https://api/api/billing/portal' && last.opts.method === 'POST' && last.opts.headers.Authorization === 'Bearer tok' && aw.location.href === 'https://customer-portal.paddle.com/o', 'account: Manage -> portal overview');
  await ad.ids.cancelSubscription.click();
  ok(aw.location.href === 'https://customer-portal.paddle.com/c', 'account: Cancel -> portal cancel page');
  ad = accountDoc(); aw = { location: { href: null } };
  A.setupBilling(ad, aw, { plan: 'pro', billing_source: 'paddle' }, 'https://api', 'tok', async () => ({ ok: false, status: 502 }));
  await ad.ids.manageSubscription.click();
  ok(aw.location.href === null && !ad.ids.billingError.hidden && !ad.ids.manageSubscription.disabled, 'account: portal error shown, button usable again');
  ad = accountDoc(); aw = { location: { href: null } };
  A.setupBilling(ad, aw, { plan: 'pro', billing_source: 'paddle' }, 'https://api', 'tok', async () => ({ ok: true, json: async () => ({ overview_url: 'javascript:alert(1)' }) }));
  await ad.ids.manageSubscription.click();
  ok(aw.location.href === null, 'account: non-https portal link refused');

  console.log(`\n${passed} passed`);
})().catch(e => { console.error(e.message); process.exit(1); });
