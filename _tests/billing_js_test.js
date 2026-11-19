// Tests for billing.js and the subscription buttons in account.js.
// Run from website/:  node _tests/billing_js_test.js
'use strict';
const path = require('path');
const fs = require('fs');
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
  // the page's own button: disabled until Paddle.js is ready
  const label = { en: 'Subscribe – $4.99/month', fr: 'S’abonner – $4.99/mois', ar: 'اشترك – $4.99 شهريًا' }[lang];
  const placeholder = el('button', { id: 'proCta', text: label });
  placeholder.disabled = true;
  card.appendChild(placeholder);
  const price = el('span', { text: '$4.99' });
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
// paddle: a Paddle.js stub by default; false = not loaded yet (CDN tests)
function windowMock({ path = '/', search = '', hash = '', store = storageMock(), paddle } = {}) {
  const w = {
    location: { pathname: path, search, hash, hostname: 'pdf2wordly.com', href: null, replaced: null, replace(u) { this.replaced = u; } },
    sessionStorage: store,
  };
  if (paddle !== false) w.Paddle = paddle || paddleMock();
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
const meFetch = body => async () => { meFetch.calls = (meFetch.calls || 0) + 1; return { ok: true, json: async () => body }; };

(async () => {
  // ---- config in one place
  ok(B.PADDLE.env === 'production' && B.PADDLE.priceId === 'pri_01m7sv3bfjjw2sr5se9mb34p5q'
     && B.PADDLE.clientToken === 'live_7207935eda621c8816f74b80377'
     && B.PADDLE.scriptUrl === 'https://cdn.paddle.com/paddle/v2/paddle.js', 'config: LIVE env, live token, live price, Paddle.js v2 URL');
  ok(!('flagEnabled' in B) && !/paddle=sandbox|pw-paddle/.test(fs.readFileSync(path.join(__dirname, '..', 'billing.js'), 'utf8')), 'no hidden ?paddle=sandbox gate left');

  // ---- open to everyone: no URL flag, no stored flag
  let doc = homePage(); let w = windowMock();
  ok(await B.init(doc, w, signedIn, meFetch({ plan: 'free' })) === 'ready', 'checkout ready for every visitor (no ?paddle=sandbox)');
  ok(w.Paddle.env === null, 'live: Paddle.Environment.set("sandbox") is not called');

  // ---- label in en, fr, ar; Paddle initialised
  for (const [lang, label] of [['en', 'Subscribe – $4.99/month'], ['fr', 'S’abonner – $4.99/mois'], ['ar', 'اشترك – $4.99 شهريًا']]) {
    const p = paddleMock();
    doc = homePage(lang);
    w = windowMock({ path: lang === 'en' ? '/' : `/${lang}/`, paddle: p });
    ok(await B.init(doc, w, signedIn, meFetch({ plan: 'free' })) === 'ready', `${lang}: ready`);
    const btn = doc.getElementById('proCta');
    ok(btn.tagName === 'BUTTON' && btn.textContent === label, `${lang}: label "${label}"`);
    ok(p.env === null && p.init.token === 'live_7207935eda621c8816f74b80377' && typeof p.init.eventCallback === 'function', `${lang}: Paddle live + live token`);
    await btn.click();
    ok(p.opened && p.opened.items.length === 1 && p.opened.items[0].priceId === 'pri_01m7sv3bfjjw2sr5se9mb34p5q' && p.opened.items[0].quantity === 1, `${lang}: checkout with the live price, quantity 1`);
    ok(p.opened.customer.email === user.email && p.opened.customData.supabase_user_id === user.id, `${lang}: email + supabase_user_id`);
    ok(p.opened.settings.displayMode === 'overlay' && p.opened.settings.locale === lang, `${lang}: overlay, locale ${lang}`);
  }

  // ---- guard: never open the checkout for a Pro user
  {
    const p = paddleMock(); doc = homePage();
    w = windowMock({ paddle: p });
    w.pwPlan = { state: 'pro', update() {} };
    meFetch.calls = 0;
    await B.init(doc, w, signedIn, meFetch({ plan: 'free' }));
    const [res] = await doc.getElementById('proCta').click();
    ok(res === 'already-pro' && !p.opened && meFetch.calls === 0, 'guard: page already shows Pro -> no checkout');
    ok(doc.card.children[1].textContent === 'You already have Pro. Manage it from your account settings.', 'guard: "You already have Pro" shown');
  }
  {
    const p = paddleMock(); doc = homePage('fr');
    w = windowMock({ path: '/fr/', paddle: p });
    let updated = null;
    w.pwPlan = { state: 'free', update(me, uid) { updated = { me, uid }; this.state = 'pro'; } };
    await B.init(doc, w, signedIn, meFetch({ plan: 'pro', subscription_status: 'active' }));
    const [res] = await doc.getElementById('proCta').click();
    ok(res === 'already-pro' && !p.opened, 'guard: fresh /api/me says Pro -> no checkout');
    ok(updated && updated.uid === user.id && updated.me.plan === 'pro', 'guard: page plan updated from the fresh /api/me');
    ok(doc.card.children[1].textContent === 'Vous avez déjà Pro. Gérez-le depuis les paramètres du compte.', 'guard: message in French');
  }
  {
    const p = paddleMock(); doc = homePage('ar');
    w = windowMock({ path: '/ar/', paddle: p });
    await B.init(doc, w, signedIn, async () => ({ ok: false, status: 503 }));
    const [res] = await doc.getElementById('proCta').click();
    ok(res === 'check-failed' && !p.opened, 'guard: /api/me fails -> no checkout');
    ok(doc.card.children[1].textContent === 'تعذّر التحقق من خطتك. يُرجى المحاولة مرة أخرى.', 'guard: message in Arabic');
    await B.init(doc = homePage(), w = windowMock({ paddle: p }), signedIn, async () => { throw new Error('net'); });
    ok((await doc.getElementById('proCta').click())[0] === 'check-failed' && !p.opened, 'guard: network error -> no checkout');
  }
  {
    // double click: only one check / one checkout at a time
    const p = paddleMock(); doc = homePage();
    w = windowMock({ paddle: p });
    let release; let n = 0;
    const slow = async () => { n++; await new Promise(r => { release = r; }); return { ok: true, json: async () => ({ plan: 'free' }) }; };
    await B.init(doc, w, signedIn, slow);
    const btn = doc.getElementById('proCta');
    const first = btn.click();
    await new Promise(r => setTimeout(r, 0));
    const [second] = await btn.click();
    release(); await first;
    ok(second === 'busy' && n === 1, 'guard: a second click while checking is ignored');
  }

  // ---- Paddle.js loaded from the CDN when not present
  doc = homePage(); w = windowMock({ paddle: false });
  doc.head.onAppend = s => { w.Paddle = paddleMock(); s.onload(); };
  ok(await B.init(doc, w, signedIn, async () => ({})) === 'ready' && doc.head.children[0].src === B.PADDLE.scriptUrl, 'loads Paddle.js v2 from the CDN');
  doc = homePage(); w = windowMock({ paddle: false });
  doc.head.onAppend = s => s.onerror();
  ok(await B.init(doc, w, signedIn, async () => ({})) === 'error' && doc.getElementById('proCta').disabled
     && doc.getElementById('proCta').textContent === 'The checkout could not open. Please try again later.', 'Paddle.js fails: button stays disabled and says why');

  // ---- signed out: remember #pricing and go to login
  for (const [p, login, back] of [['/', 'login.html', '/#pricing'], ['/fr/', 'login.html', '/fr/#pricing'], ['/ar/', 'login.html', '/ar/#pricing']]) {
    const st = storageMock();
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
  ok(await B.init(homePage(), w, signedIn, async () => ({}), t0) === 'ready', 'broken return value ignored');

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
  w = windowMock({ paddle: p2 });
  await B.init(doc, w, signedIn, async () => ({ ok: true, json: async () => ({ plan: 'pro' }) }));
  const status = doc.card.children[1];
  const done = p2.init.eventCallback({ name: 'checkout.completed' });
  ok(status.textContent === 'Payment received, activating Pro…' && !status.hidden, 'completed: "Payment received, activating Pro…"');
  await done;
  ok(doc.badge.textContent === 'Pro' && doc.badge.getAttribute('data-plan') === 'pro' && !doc.badge.hidden, 'completed: badge refreshed to Pro');
  ok(status.textContent === 'Pro is active. Thank you!', 'completed: confirmation shown');
  await p2.init.eventCallback({ name: 'checkout.loaded' });
  ok(status.textContent === 'Pro is active. Thank you!', 'other Paddle events ignored');

  // ---- checkout.completed with plan-ui.js: header/menu/pricing updated via pwPlan.refresh
  {
    const p3 = paddleMock(); doc = homePage();
    w = windowMock({ paddle: p3 });
    let refreshed = 0;
    w.pwPlan = { state: 'free', refresh: async () => { refreshed++; w.pwPlan.state = 'pro'; return { plan: 'pro' }; }, update() {} };
    await B.init(doc, w, signedIn, meFetch({ plan: 'pro' }));
    await p3.init.eventCallback({ name: 'checkout.completed' });
    ok(refreshed === 1 && w.pwPlan.state === 'pro', 'completed: pwPlan.refresh() called once, no reload');
    ok(doc.badge.hidden, 'completed: plan-ui owns the badge when present');
  }

  // ---- account.js
  const d = A.describe;
  ok(d({ plan: 'pro', subscription_status: 'active', renews_at: '2026-12-18T00:00:00Z' }).status === 'Active, renews on December 18, 2026', 'account: renews on');
  ok(d({ plan: 'pro', subscription_status: 'active', cancel_scheduled: true, ends_at: '2026-12-18T00:00:00Z' }).status === 'Cancellation scheduled: Pro until December 18, 2026', 'account: cancellation scheduled');
  ok(d({ plan: 'pro', subscription_status: 'canceled', ends_at: '2026-12-18T00:00:00Z' }).status === 'Cancelled: Pro until December 18, 2026', 'account: cancelled, Pro until');
  ok(d({ plan: 'pro', subscription_status: 'past_due' }).plan === 'Pro', 'account: past_due still Pro');

  function accountDoc() {
    const ids = {};
    ['billingActions', 'billingError', 'manageSubscription', 'cancelSubscription', 'billingNote'].forEach(id => { ids[id] = el('div', { id }); });
    ids.billingActions.hidden = true; ids.billingError.hidden = true; ids.billingNote.hidden = true;
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

  // ---- cancellation scheduled: only "Manage subscription" + the note (en, fr, ar via pwT)
  {
    const fs = require('fs');
    const src = fs.readFileSync(path.join(__dirname, '..', 'script.js'), 'utf8');
    const start = src.indexOf('{', src.indexOf('const pwI18n='));
    let depth = 0, end = start, inStr = false, esc = false;
    for (let i = start; i < src.length; i++) { const ch = src[i];
      if (inStr) { if (esc) esc = false; else if (ch === '\\') esc = true; else if (ch === '"') inStr = false; continue; }
      if (ch === '"') inStr = true; else if (ch === '{') depth++; else if (ch === '}' && --depth === 0) { end = i; break; } }
    const DICT = JSON.parse(src.slice(start, end + 1));
    const NOTE = 'Your subscription will not renew. You can resume it from Manage subscription.';
    for (const [lang, text] of [['en', NOTE], ['fr', 'Votre abonnement ne sera pas renouvelé. Vous pouvez le réactiver depuis Gérer l’abonnement.'], ['ar', 'لن يتجدد اشتراكك. يمكنك استئنافه من إدارة الاشتراك.']]) {
      ad = accountDoc(); aw = { location: { href: null }, pwT: k => (DICT[lang] && DICT[lang][k]) || k };
      A.setupBilling(ad, aw, { plan: 'pro', billing_source: 'paddle', cancel_scheduled: true, ends_at: '2026-12-18T00:00:00Z' }, 'https://api', 'tok', portalFetch);
      ok(!ad.ids.billingActions.hidden && !ad.ids.manageSubscription.hidden && ad.ids.cancelSubscription.hidden, `scheduled (${lang}): Cancel hidden, Manage kept`);
      ok(!ad.ids.billingNote.hidden && ad.ids.billingNote.textContent === text, `scheduled (${lang}): note "${text.slice(0, 40)}…"`);
    }
    const RENEWS = 'Your subscription renews automatically every month until you cancel it.';
    for (const [lang, text] of [['en', RENEWS], ['fr', 'Votre abonnement se renouvelle automatiquement chaque mois jusqu’à ce que vous le résiliiez.'], ['ar', 'يتجدد اشتراكك تلقائيًا كل شهر إلى أن تلغيه.']]) {
      ad = accountDoc(); aw = { location: { href: null }, pwT: k => (DICT[lang] && DICT[lang][k]) || k };
      A.setupBilling(ad, aw, { plan: 'pro', billing_source: 'paddle', subscription_status: 'active', cancel_scheduled: false }, 'https://api', 'tok', portalFetch);
      ok(!ad.ids.cancelSubscription.hidden && !ad.ids.billingNote.hidden && ad.ids.billingNote.textContent === text, `renewing (${lang}): "${text.slice(0, 40)}…"`);
    }
    for (const st of ['trialing', 'past_due']) {
      ad = accountDoc(); aw = { location: { href: null } };
      A.setupBilling(ad, aw, { plan: 'pro', billing_source: 'paddle', subscription_status: st, cancel_scheduled: false }, 'https://api', 'tok', portalFetch);
      ok(ad.ids.billingNote.textContent === RENEWS, `renewing (${st}): renewal line`);
    }
    ad = accountDoc(); aw = { location: { href: null } };
    A.setupBilling(ad, aw, { plan: 'pro', billing_source: 'paddle', subscription_status: 'active', cancel_scheduled: true }, 'https://api', 'tok', portalFetch);
    ok(ad.ids.billingNote.textContent === NOTE && !ad.ids.billingNote.textContent.includes('renews automatically'), 'scheduled: only "will not renew", no renewal line');
    ad = accountDoc(); aw = { location: { href: null } };
    A.setupBilling(ad, aw, { plan: 'pro', billing_source: 'paddle', subscription_status: 'canceled', cancel_scheduled: false }, 'https://api', 'tok', portalFetch);
    ok(ad.ids.billingNote.hidden && ad.ids.billingNote.textContent === '', 'canceled (Pro until end): no renewal line');
    ad = accountDoc(); aw = { location: { href: null } };
    A.setupBilling(ad, aw, { plan: 'pro', billing_source: 'paddle', cancel_scheduled: false }, 'https://api', 'tok', portalFetch);
    ok(!ad.ids.cancelSubscription.hidden && ad.ids.billingNote.hidden && ad.ids.billingNote.textContent === '', 'no status: Cancel shown, no note');
    ad = accountDoc(); aw = { location: { href: null } };
    A.setupBilling(ad, aw, { plan: 'free', cancel_scheduled: true }, 'https://api', 'tok', portalFetch);
    ok(ad.ids.billingActions.hidden && ad.ids.billingNote.hidden, 'free: no buttons, no note');
    const page = fs.readFileSync(path.join(__dirname, '..', 'account.html'), 'utf8');
    ok(page.includes('<p>You can cancel at any time:</p>') && !/not available yet/i.test(page), 'account.html: "You can cancel at any time:", no "not available yet"');
    ok(/<\/dl>\s*<p class="account-note" id="billingNote" hidden><\/p>/.test(page), 'account.html: note line right under the status');
    const css = fs.readFileSync(path.join(__dirname, '..', 'style.css'), 'utf8');
    ok(css.includes('.account-actions>[hidden]{display:none}'), 'css: hidden button really hidden (beats .btn display)');
  }

  console.log(`\n${passed} passed`);
})().catch(e => { console.error(e.message); process.exit(1); });
