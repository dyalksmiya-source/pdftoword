// Tests for the account deletion UI (account.js setupDelete), the
// "account deleted" notice (plan-ui.js) and the delete-account pages.
// Run from website/:  node _tests/account_delete_test.js
'use strict';
const fs = require('fs');
const path = require('path');
const SITE = path.join(__dirname, '..');
const A = require(path.join(SITE, 'account.js'));
const P = require(path.join(SITE, 'plan-ui.js'));

let passed = 0;
const ok = (c, m) => { if (!c) throw new Error('FAIL: ' + m); passed++; console.log('ok -', m); };
const read = f => fs.readFileSync(path.join(SITE, f), 'utf8');

// ---- tiny DOM
function el(tag, id) {
  return {
    tagName: tag.toUpperCase(), id, children: [], listeners: {}, attrs: {}, hidden: false, disabled: false, value: '', textContent: '',
    addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); },
    fire(t, ev) { return Promise.all((this.listeners[t] || []).map(f => f(ev || { preventDefault() {} }))); },
    appendChild(c) { this.children.push(c); c.parentNode = this; return c; },
    setAttribute(k, v) { this.attrs[k] = String(v); }, removeAttribute(k) { delete this.attrs[k]; },
    focus() { this.focused = true; },
    click() { this.clicked = (this.clicked || 0) + 1; },
  };
}
function accountDoc() {
  const ids = {};
  for (const [tag, id] of [['button', 'deleteAccountBtn'], ['dialog', 'deleteDialog'], ['form', 'deleteForm'], ['input', 'deleteConfirmInput'],
    ['button', 'deleteConfirm'], ['button', 'deleteCancel'], ['p', 'deleteDialogError'], ['p', 'deleteError'], ['button', 'manageSubscription']]) ids[id] = el(tag, id);
  ids.deleteDialog.showModal = function () { this.open = true; };
  ids.deleteDialog.close = function () { this.open = false; };
  ids.deleteDialogError.hidden = true; ids.deleteError.hidden = true;
  return {
    ids,
    getElementById: id => ids[id] || null,
    createElement: tag => el(tag),
    createTextNode: t => ({ textContent: t, text: true }),
  };
}
const text = node => node.children.length ? node.children.map(c => c.textContent).join('') : node.textContent;
function storage() { const m = {}; return { m, getItem: k => m[k] ?? null, setItem: (k, v) => { m[k] = String(v); }, removeItem: k => { delete m[k]; } }; }
function makeWin() {
  const local = storage(); local.setItem('sb-yjxfxdbrjsiuevnkiiak-auth-token', '{"user":{"id":"u"}}');
  const session = storage(); session.setItem('pw-plan', '{"u":"u","p":"pro"}');
  return { localStorage: local, sessionStorage: session, location: { replaced: null, replace(u) { this.replaced = u; } } };
}
function client() {
  const c = { signedOut: null, auth: { signOut: async o => { c.signedOut = o; } } };
  return c;
}
const answer = (status, body = {}) => async (url, opts) => { answer.last = { url, opts }; return { ok: status >= 200 && status < 300, status, json: async () => body }; };

(async () => {
  // ---- dialog opens, confirm only with exactly DELETE
  let doc = accountDoc(); let w = makeWin(); let c = client();
  let h = A.setupDelete(doc, w, c, 'https://api', async () => 'tok', answer(200, { deleted: true }), {});
  ok(h, 'setupDelete wired');
  await doc.ids.deleteAccountBtn.fire('click');
  ok(doc.ids.deleteDialog.open && doc.ids.deleteConfirm.disabled && doc.ids.deleteConfirmInput.focused, 'open: dialog shown, confirm disabled, input focused');
  for (const v of ['delete', 'DELETE ', 'DEL', '']) {
    doc.ids.deleteConfirmInput.value = v; await doc.ids.deleteConfirmInput.fire('input');
    ok(doc.ids.deleteConfirm.disabled, `confirm stays disabled for "${v}"`);
  }
  doc.ids.deleteConfirmInput.value = 'delete';
  ok(await h.submit() === 'not-confirmed' && !answer.last, 'submit without exact DELETE: no request');
  doc.ids.deleteConfirmInput.value = 'DELETE'; await doc.ids.deleteConfirmInput.fire('input');
  ok(!doc.ids.deleteConfirm.disabled, 'confirm enabled for "DELETE"');

  // ---- success
  ok(await h.submit() === 'deleted', 'success: deleted');
  ok(answer.last.url === 'https://api/api/account' && answer.last.opts.method === 'DELETE'
     && answer.last.opts.headers.Authorization === 'Bearer tok' && JSON.parse(answer.last.opts.body).confirm === 'DELETE', 'DELETE /api/account with token + {"confirm":"DELETE"}');
  ok(c.signedOut && c.signedOut.scope === 'local', 'success: local sign-out');
  ok(!('sb-yjxfxdbrjsiuevnkiiak-auth-token' in w.localStorage.m) && !('pw-plan' in w.sessionStorage.m), 'success: session and cached plan removed');
  ok(w.location.replaced === 'index.html?account=deleted', 'success: redirect to home with the notice');

  // ---- 409 renewing subscription
  doc = accountDoc(); w = makeWin(); answer.last = null;
  h = A.setupDelete(doc, w, client(), 'https://api', async () => 'tok', answer(409, { code: 'SUBSCRIPTION_ACTIVE' }), {});
  await doc.ids.deleteAccountBtn.fire('click');
  doc.ids.deleteConfirmInput.value = 'DELETE';
  ok(await h.submit() === 'blocked' && !doc.ids.deleteDialog.open && w.location.replaced === null, '409: dialog closed, nothing deleted');
  const err = doc.ids.deleteError;
  ok(!err.hidden && text(err) === 'You have a Pro subscription that is still active or paused. Cancel it first in Manage subscription, then delete your account.', '409: cancel-first message');
  const link = err.children.find(n => n.tagName === 'A');
  await link.fire('click');
  ok(link.href === '#billingActions' && doc.ids.manageSubscription.clicked === 1, '409: link opens Manage subscription');

  // ---- other errors stay in the dialog, button usable again
  for (const [status, expected, msg] of [[401, 'expired', 'Your session has expired'], [429, 'limited', 'Too many attempts'], [500, 'failed', "We couldn't delete"]]) {
    doc = accountDoc(); w = makeWin();
    h = A.setupDelete(doc, w, client(), 'https://api', async () => 'tok', answer(status), {});
    await doc.ids.deleteAccountBtn.fire('click');
    doc.ids.deleteConfirmInput.value = 'DELETE';
    ok(await h.submit() === expected && doc.ids.deleteDialogError.textContent.startsWith(msg) && !doc.ids.deleteDialogError.hidden
       && !doc.ids.deleteConfirm.disabled && doc.ids.deleteConfirm.textContent === 'Delete my account' && w.location.replaced === null,
       `${status}: "${msg}…", can retry`);
  }
  doc = accountDoc(); w = makeWin();
  h = A.setupDelete(doc, w, client(), 'https://api', async () => 'tok', async () => { throw new Error('net'); }, {});
  doc.ids.deleteConfirmInput.value = 'DELETE';
  ok(await h.submit() === 'failed', 'network error: failed, nothing deleted');
  doc = accountDoc(); w = makeWin(); answer.last = null;
  h = A.setupDelete(doc, w, client(), 'https://api', async () => null, answer(200), {});
  doc.ids.deleteConfirmInput.value = 'DELETE';
  ok(await h.submit() === 'expired' && !answer.last, 'no session token: no request');

  // ---- double submit
  doc = accountDoc(); w = makeWin();
  let calls = 0; let release;
  const slow = async () => { calls++; await new Promise(r => { release = r; }); return { ok: true, status: 200, json: async () => ({}) }; };
  h = A.setupDelete(doc, w, client(), 'https://api', async () => 'tok', slow, {});
  doc.ids.deleteConfirmInput.value = 'DELETE';
  const first = h.submit();
  await new Promise(r => setTimeout(r, 0));
  ok(await h.submit() === 'not-confirmed' && calls === 1, 'second submit while deleting is ignored');
  release(); await first;

  // ---- account page sign-out listener does not send a deleted user to login
  const src = read('account.js');
  ok(/if \(!s && !deleteState\.deleted\) win\.location\.replace\(loginUrl\)/.test(src), 'auth listener skips the login redirect after a deletion');

  // ---- notice on the home page
  const DICT = (() => {
    const s = read('script.js'); const start = s.indexOf('{', s.indexOf('const pwI18n='));
    let d = 0, q = false, e = false;
    for (let i = start; i < s.length; i++) { const ch = s[i];
      if (q) { if (e) e = false; else if (ch === '\\') e = true; else if (ch === '"') q = false; continue; }
      if (ch === '"') q = true; else if (ch === '{') d++; else if (ch === '}' && --d === 0) return JSON.parse(s.slice(start, i + 1)); }
  })();
  for (const [lang, msg] of [['en', 'Your account has been deleted.'], ['fr', 'Votre compte a été supprimé.'], ['ar', 'تم حذف حسابك.']]) {
    const main = el('main', 'main'); const body = el('body'); body.appendChild(main);
    const inserted = [];
    main.parentNode.insertBefore = (n, ref) => { inserted.push(n); n.parentNode = body; };
    const ndoc = { getElementById: id => (id === 'main' ? main : null), createElement: t => el(t), body };
    let replaced = null;
    const nwin = { location: { search: '?account=deleted&x=1', pathname: '/', hash: '' }, history: { replaceState: (a, b, u) => { replaced = u; } },
      pwT: k => (DICT[lang] && DICT[lang][k]) || k };
    ok(P.showAccountNotice(ndoc, nwin) === true, `notice (${lang}) shown`);
    const box = inserted[0];
    ok(box.className === 'site-notice' && box.attrs.role === 'status' && box.children[0].textContent === msg, `notice (${lang}): "${msg}"`);
    ok(replaced === '/?x=1', `notice (${lang}): ?account=deleted removed from the URL`);
  }
  ok(P.showAccountNotice({}, { location: { search: '' } }) === false, 'no notice without ?account=deleted');

  // ---- account.html
  const acct = read('account.html');
  ok(!/Coming soon/.test(acct.slice(acct.indexOf('id="delete-title"'), acct.indexOf('</dialog>'))), 'account.html: no "Coming soon" in the delete section');
  ok(acct.includes('id="deleteAccountBtn">Delete account</button>') && acct.includes('<dialog class="confirm-dialog" id="deleteDialog"')
     && acct.includes('Type <strong>DELETE</strong> to confirm') && acct.includes('the remaining Pro time is lost'), 'account.html: button, dialog, DELETE, lost Pro time');

  // ---- help pages and account page: paused must be cancelled first
  ok(read('delete-account.html').includes('A paused subscription must be cancelled too') && !read('delete-account.html').includes('cancelled, paused or set to end'), 'delete-account.html (en): paused must be cancelled first');
  ok(read('fr/delete-account.html').includes('Un abonnement suspendu doit aussi être résilié') && !read('fr/delete-account.html').includes('résilié, suspendu ou'), 'delete-account.html (fr): paused must be cancelled first');
  ok(read('ar/delete-account.html').includes('يجب إلغاء الاشتراك الموقوف مؤقتًا أيضًا') && !read('ar/delete-account.html').includes('ملغًى أو موقوفًا مؤقتًا أو'), 'delete-account.html (ar): paused must be cancelled first');
  ok(acct.includes('still active or paused, cancel it first') && read('privacy.html').includes('still active or paused, cancel it first') && read('terms.html').includes('still active or paused must be cancelled first'), 'account, privacy, terms: "active or paused"');

  // ---- public pages
  const pages = [['delete-account.html', 'en', ''], ['fr/delete-account.html', 'fr', '../'], ['ar/delete-account.html', 'ar', '../']];
  for (const [f, lang, pre] of pages) {
    const s = read(f);
    const url = `https://pdf2wordly.com/${lang === 'en' ? '' : lang + '/'}delete-account.html`;
    ok(s.includes(`<html lang="${lang}"`) && (lang !== 'ar' || s.includes('dir="rtl"')), `${f}: lang ${lang}`);
    ok(s.includes(`<link rel="canonical" href="${url}">`) && (s.match(/rel="canonical"/g) || []).length === 1, `${f}: one canonical`);
    ok(['en', 'fr', 'ar'].every(l => s.includes(`hreflang="${l}" href="https://pdf2wordly.com/${l === 'en' ? '' : l + '/'}delete-account.html"`))
       && s.includes('hreflang="x-default" href="https://pdf2wordly.com/delete-account.html"'), `${f}: hreflang en/fr/ar/x-default`);
    ok(!/noindex/i.test(s), `${f}: indexable`);
    ok(s.includes('mailto:support@pdf2wordly.com') && s.includes(`href="${pre}account.html"`) && s.includes('Paddle') && s.includes('DELETE'), `${f}: steps, Paddle, email fallback`);
    ok(['website', 'app', 'pro', 'deleted', 'kept', 'email'].every(id => s.includes(`<article id="${id}" class="legal-section">`)), `${f}: 6 sections`);
    ok(s.includes('href="/delete-account.html"') && s.includes('href="/fr/delete-account.html"') && s.includes('href="/ar/delete-account.html"'), `${f}: language switcher points to this page`);
    const ldTags = s.match(/<script type="application\/ld\+json"[^>]*>/g) || [];
    ok(ldTags.length === 1 && ldTags[0].includes('data-seo="site"') && !s.includes('"FAQPage"') && !s.includes('WebApplication'), `${f}: only the site-wide structured data (Organization + WebSite)`);
    ok(!s.includes('billing.js" defer') && !s.includes('id="proCta"') && !s.includes('id="converter"'), `${f}: no home-page leftovers`);
  }

  // ---- footer link on every page, next to Privacy
  const all = fs.readdirSync(SITE).filter(f => f.endsWith('.html')).concat(['fr/index.html', 'ar/index.html', 'fr/delete-account.html', 'ar/delete-account.html'])
    .filter(f => read(f).includes('class="footer-legal"'));
  ok(all.length === 28, 'pages with the legal footer: 28 (incl. 404.html)');
  for (const f of all) {
    const nav = read(f).match(/<nav class="footer-legal"[^>]*>.*?<\/nav>/)[0];
    ok(/privacy\.html">[^<]*<\/a><a href="\/?delete-account\.html">(Delete account|Supprimer le compte|حذف الحساب)<\/a>/.test(nav), `${f}: footer "Delete account" after Privacy`);
  }

  // ---- privacy / terms / sitemap
  const privacy = read('privacy.html');
  const del = privacy.slice(privacy.indexOf('id="account-deletion"'), privacy.indexOf('id="your-rights"'));
  ok(!/coming soon/i.test(del) && del.includes('href="account.html"') && del.includes('href="delete-account.html"') && del.includes('Paddle'), 'privacy.html: deletion section updated');
  ok(!/coming soon/i.test(read('terms.html').match(/id="termination"[\s\S]*?<\/article>/)[0]), 'terms.html: no "coming soon" for deletion');
  const sm = read('sitemap.xml');
  ok(['', 'fr/', 'ar/'].every(p => sm.includes(`<loc>https://pdf2wordly.com/${p}delete-account.html</loc>`)), 'sitemap: 3 delete-account URLs');

  console.log(`\n${passed} passed`);
})().catch(e => { console.error(e.message); process.exit(1); });
