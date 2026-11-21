// app-captcha.html: the Turnstile page the Android app opens in a WebView.
// Runs the page script with a fake window/document.
// Run from website/:  node _tests/app_captcha_test.js
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const SITE = path.join(__dirname, '..');
const read = f => fs.readFileSync(path.join(SITE, f), 'utf8');

let passed = 0;
const ok = (c, m) => { if (!c) throw new Error('FAIL: ' + m); passed++; console.log('ok -', m); };

const html = read('app-captcha.html');
const authKey = /var TURNSTILE_SITE_KEY = '([^']+)'/.exec(read('auth.js'))[1];

// ---- static rules
ok(html.includes(`var SITE_KEY = '${authKey}';`), 'same public Turnstile site key as auth.js');
ok(html.includes('<meta name="robots" content="noindex, follow">'), 'noindex');
ok(!read('sitemap.xml').includes('app-captcha'), 'not in the sitemap');
const seo = JSON.parse(read('_tools/seo.json'));
ok(seo.pages['app-captcha.html'] && seo.pages['app-captcha.html'].index === false, 'seo.json: index false');
const urls = (html.match(/https?:\/\/[^\s'"<>)]+/g) || []).filter(u => !u.startsWith('https://pdf2wordly.com'));
ok(urls.length === 1 && urls[0].startsWith('https://challenges.cloudflare.com/turnstile/v0/api.js'),
   'the only outside resource is the Turnstile script: ' + urls.join(', '));
ok(!/secret/i.test(html.replace(/No secret here/g, '')), 'no secret on the page');
ok(!/localStorage|sessionStorage|document\.cookie/.test(html), 'stores nothing in the browser');

// ---- run the page script
const script = html.slice(html.indexOf('<script>') + 8, html.indexOf('</script>'));
function run(search, bridge) {
  const attrs = {};
  const added = [];
  const msg = { textContent: '' };
  const doc = {
    documentElement: { lang: '', dir: '', setAttribute: (k, v) => { attrs[k] = v; } },
    getElementById: id => (id === 'msg' ? msg : null),
    createElement: () => ({}),
    head: { appendChild: el => added.push(el) },
  };
  const posted = [];
  const win = { location: { search } };
  if (bridge) win.pdf2wordlyBridge = { postMessage: s => posted.push(JSON.parse(s)) };
  const rendered = [];
  win.turnstile = { render: (sel, opts) => { rendered.push([sel, opts]); } };
  vm.runInNewContext(script, { window: win, document: doc, URLSearchParams, JSON, String });
  return { attrs, added, msg, posted, rendered, win, doc };
}

let r = run('?lang=ar&theme=dark', false);
ok(r.added.length === 0 && r.msg.textContent.includes('PDF2Wordly'), 'outside the app: no widget, a short note');

r = run('?lang=ar&theme=dark', true);
ok(r.added.length === 1 && r.added[0].src.includes('render=explicit') && r.added[0].src.includes('onload=onPwAppTurnstile'),
   'in the app: loads Turnstile explicitly');
ok(r.doc.documentElement.dir === 'rtl' && r.doc.documentElement.lang === 'ar' && r.attrs['data-theme'] === 'dark',
   'Arabic is right to left, dark theme applied');
r.win.onPwAppTurnstile();
const opts = r.rendered[0][1];
ok(opts.sitekey === authKey && opts.language === 'ar' && opts.theme === 'dark', 'widget gets key, language and theme');
opts.callback('0.tok-123');
ok(r.posted.length === 1 && r.posted[0].type === 'token' && r.posted[0].token === '0.tok-123', 'token goes to the bridge');
opts['expired-callback']();
opts['error-callback']('110200');
ok(r.posted[1].type === 'expired' && r.posted[2].type === 'error' && r.posted[2].code === '110200', 'expired and error are reported');
r.added[0].onerror();
ok(r.posted[3].type === 'error' && r.posted[3].code === 'load', 'script load failure is reported');

r = run('?lang=xx&theme=<script>', true);
r.win.onPwAppTurnstile();
ok(r.rendered[0][1].language === 'en' && r.rendered[0][1].theme === 'light' && r.doc.documentElement.dir === 'ltr',
   'unknown lang/theme fall back to English, light');

console.log(`\n${passed} passed`);
