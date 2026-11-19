// Tests for auth.js (login, sign up, 6-digit code, Google, forgot password,
// Turnstile) and the login pages in en / fr / ar.
// Run from website/:  node _tests/auth_test.js
'use strict';
const fs = require('fs');
const path = require('path');
const SITE = path.join(__dirname, '..');
const A = require(path.join(SITE, 'auth.js'));

let passed = 0;
const ok = (c, m) => { if (!c) throw new Error('FAIL: ' + m); passed++; console.log('ok -', m); };
const read = f => fs.readFileSync(path.join(SITE, f), 'utf8');

// ---- tiny DOM ------------------------------------------------------------------
function el(id, extra) {
  const e = {
    id, value: '', hidden: false, disabled: false, textContent: '', checked: false, attrs: {}, listeners: {}, children: [], classes: new Set(),
    classList: { toggle(c, on) { on ? e.classes.add(c) : e.classes.delete(c); } },
    setAttribute(k, v) { e.attrs[k] = String(v); }, getAttribute(k) { return k in e.attrs ? e.attrs[k] : null; },
    addEventListener(t, f) { (e.listeners[t] = e.listeners[t] || []).push(f); },
    fire(t, ev) { return Promise.all((e.listeners[t] || []).map(f => f(ev || { preventDefault() {} }))); },
    appendChild(c) { e.children.push(c); return c; },
    focus() { e.focused = true; }, select() { e.selected = true; },
    ...extra,
  };
  Object.defineProperty(e, 'textContent', {
    get() { return e._text !== undefined ? e._text : e.children.map(c => c.textContent).join(''); },
    set(v) { e._text = v; if (v === '') { e.children = []; e._text = undefined; } },
  });
  return e;
}
const IDS = ['authTabs', 'tabLogin', 'tabSignup', 'panelLogin', 'panelSignup', 'panelCode', 'authSwitch', 'loginMessage', 'signupMessage', 'codeMessage',
  'loginForm', 'signupForm', 'codeForm', 'loginEmail', 'loginPassword', 'loginEmailField', 'loginPasswordField', 'loginSubmit',
  'signupName', 'signupEmail', 'signupPassword', 'signupNameField', 'signupEmailField', 'signupPasswordField', 'signupAgree', 'signupSubmit',
  'codeInput', 'codeEmail', 'verifyCode', 'resendCode', 'changeEmail'];
function makeDoc(lang = 'en') {
  const ids = {};
  IDS.forEach(id => { ids[id] = el(id); });
  const googles = [el('g1'), el('g2')];
  return {
    ids, googles,
    documentElement: { getAttribute: k => (k === 'lang' ? lang : null) },
    getElementById: id => ids[id] || null,
    querySelectorAll: sel => (sel === '.btn-google' ? googles : []),
    createElement: () => el(),
    createTextNode: t => ({ textContent: t }),
  };
}
function makeWin({ search = '', ret = null } = {}) {
  const store = {};
  if (ret) store['pw-return'] = JSON.stringify(ret);
  const timers = [];
  return {
    timers,
    location: { search, replaced: null, replace(u) { this.replaced = u; } },
    sessionStorage: { getItem: k => store[k] ?? null, setItem: (k, v) => { store[k] = v; }, removeItem: k => { delete store[k]; } },
    setInterval(fn) { timers.push({ fn, on: true }); return timers.length - 1; },
    clearInterval(i) { if (timers[i]) timers[i].on = false; },
    tick(n = 1) { for (let k = 0; k < n; k++) timers.filter(t => t.on).forEach(t => t.fn()); },
  };
}
function makeClient(answers = {}) {
  const calls = [];
  const fn = name => async arg => { calls.push([name, arg]); const a = answers[name]; return typeof a === 'function' ? a(arg, calls) : (a || { data: {}, error: null }); };
  return { calls, auth: {
    signInWithOAuth: fn('signInWithOAuth'), signInWithPassword: fn('signInWithPassword'), signUp: fn('signUp'),
    verifyOtp: fn('verifyOtp'), resend: fn('resend'), resetPasswordForEmail: async (email, opts) => { calls.push(['resetPasswordForEmail', email, opts]); return answers.resetPasswordForEmail || { error: null }; },
  } };
}
const OFF = A.createCaptcha(null, null, '');
function setup({ lang = 'en', answers, search, ret, clock } = {}) {
  const doc = makeDoc(lang); const win = makeWin({ search, ret }); const client = makeClient(answers);
  const t = { now: clock || 1_000_000 };
  const h = A.initLogin(doc, win, client, { captcha: OFF, now: () => t.now });
  return { doc, win, client, h, t, $: doc.ids };
}
function fillSignup($, { name = 'Eve', email = 'Eve@Example.com ', pw = 'secret123', agree = true } = {}) {
  $.signupName.value = name; $.signupEmail.value = email; $.signupPassword.value = pw; $.signupAgree.checked = agree;
}
const visible = $ => ['panelLogin', 'panelSignup', 'panelCode'].filter(k => !$[k].hidden);

(async () => {
  // ---- helpers
  ok(A.cleanCode(' 12-34 56 ') === '123456' && A.cleanCode('١٢٣٤٥٦') === '123456' && A.cleanCode('۱۲۳') === '123' && A.cleanCode('1234567') === '123456' && A.cleanCode(null) === '', 'cleanCode: spaces, dashes, Arabic digits, max 6');
  const C = A.classifyError;
  ok(C({ status: 429, message: 'x' }) === 'rate_limit' && C({ code: 'over_email_send_rate_limit' }) === 'rate_limit' && C({ message: 'For security purposes, you can only request this after 47 seconds. Too many requests' }) === 'rate_limit', 'classify: rate limit');
  ok(C({ name: 'AuthRetryableFetchError', message: 'Failed to fetch' }) === 'network', 'classify: network');
  ok(C({ code: 'otp_expired', message: 'Token has expired or is invalid' }) === 'otp' && C({ message: 'Token has expired or is invalid' }) === 'otp', 'classify: wrong/expired code');
  ok(C({ code: 'email_not_confirmed' }) === 'not_confirmed' && C({ message: 'Email not confirmed' }) === 'not_confirmed', 'classify: email not confirmed');
  ok(C({ code: 'invalid_credentials' }) === 'invalid_credentials' && C({ message: 'Invalid login credentials' }) === 'invalid_credentials', 'classify: wrong password');
  ok(C({ code: 'user_already_exists' }) === 'already_registered' && C({ message: 'User already registered' }) === 'already_registered', 'classify: already registered');
  ok(C({ code: 'captcha_failed' }) === 'captcha' && C({ code: 'weak_password' }) === 'weak_password' && C({ message: '???' }) === 'other' && C(null) === null, 'classify: captcha, weak password, other, none');

  // ---- Google
  const g = A.googleOptions('en');
  ok(g.provider === 'google' && g.options.redirectTo === 'https://pdf2wordly.com/' && g.options.queryParams.prompt === 'select_account', 'Google: prompt=select_account, redirect to pdf2wordly.com');
  for (const l of ['fr', 'ar']) {
    const S2 = setup({ lang: l });
    await S2.doc.googles[0].fire('click');
    ok(S2.client.calls[0][1].options.redirectTo === `https://pdf2wordly.com/${l}/` && S2.client.calls[0][1].options.queryParams.prompt === 'select_account', `Google from ${l}/login.html returns to /${l}/`);
  }
  let S = setup();
  await S.doc.googles[1].fire('click');
  ok(S.client.calls[0][0] === 'signInWithOAuth' && S.client.calls[0][1].options.queryParams.prompt === 'select_account', 'Google button (sign up tab) uses the account chooser');
  S = setup({ answers: { signInWithOAuth: { error: { status: 500, message: 'x' } } } });
  ok(await S.h.google() === 'error' && !S.$.loginMessage.hidden, 'Google error shown on the page (no alert)');

  // ---- sign up, Confirm email OFF: session -> signed in, home in page language
  for (const [lang, home] of [['en', '/'], ['fr', '/fr/'], ['ar', '/ar/']]) {
    S = setup({ lang, answers: { signUp: { data: { session: { access_token: 't' }, user: { id: 'u' } }, error: null } } });
    fillSignup(S.$);
    ok(await S.h.signup() === 'signed-in' && S.win.location.replaced === home, `sign up with session (${lang}) -> ${home}`);
  }
  const su = S.client.calls[0][1];
  ok(su.email === 'eve@example.com' && su.password === 'secret123' && su.options.data.full_name === 'Eve' && !('captchaToken' in su.options), 'signUp: trimmed lowercase email, name, no captchaToken while Turnstile is off');

  // ---- sign up, Confirm email ON: code step + 60 s countdown
  S = setup({ answers: { signUp: { data: { session: null, user: { id: 'u', identities: [{ id: 'i' }] } }, error: null } } });
  fillSignup(S.$);
  ok(await S.h.signup() === 'code', 'sign up without session -> code step');
  ok(visible(S.$).join() === 'panelCode' && S.$.authTabs.hidden && S.$.authSwitch.hidden, 'code step alone (tabs and switch hidden)');
  ok(S.$.codeEmail.textContent === 'eve@example.com' && S.$.codeInput.focused, 'code step: email shown, input focused');
  ok(S.$.resendCode.disabled && S.$.resendCode.textContent === 'Resend code (60 s)', 'resend: 60 s countdown');
  S.win.tick(1);
  ok(S.$.resendCode.textContent === 'Resend code (59 s)', 'resend: counts down');
  ok(await S.h.resend() === 'wait' && !S.client.calls.some(c => c[0] === 'resend'), 'resend during the countdown does nothing');
  S.win.tick(59);
  ok(!S.$.resendCode.disabled && S.$.resendCode.textContent === 'Resend code', 'resend: enabled after 60 s');

  // typing / paste / auto-submit
  S.$.codeInput.value = '12a 3';
  ok(S.h.onCodeInput() === null && S.$.codeInput.value === '123', 'typing: only digits kept, no submit before 6');
  S.$.codeInput.value = '123456';
  const r1 = await S.h.onCodeInput();
  const vo = S.client.calls.find(c => c[0] === 'verifyOtp')[1];
  ok(vo.email === 'eve@example.com' && vo.token === '123456' && vo.type === 'signup', 'auto-submit on 6 digits: verifyOtp(email, token, type signup)');

  // ---- verify success -> pw-return target
  S = setup({ ret: { p: '/fr/#pricing', t: 1_000_000 - 60_000 }, answers: {
    signUp: { data: { session: null, user: { identities: [{}] } }, error: null },
    verifyOtp: { data: { session: { access_token: 't' } }, error: null } } });
  fillSignup(S.$); await S.h.signup();
  const pasted = { clipboardData: { getData: () => 'Your code: 654 321' }, preventDefault() { this.prevented = true; } };
  ok(await S.h.onCodePaste(pasted) === 'signed-in' && pasted.prevented && S.$.codeInput.value === '654321', 'paste of the full code (with text and spaces) submits');
  ok(S.win.location.replaced === '/fr/#pricing', 'signed in -> back to the saved pricing section (pw-return)');
  ok(A.returnTarget({ documentElement: { getAttribute: () => 'ar' } }, { sessionStorage: { getItem: () => JSON.stringify({ p: 'https://evil.example', t: 1 }) } }, 2) === '/ar/', 'unsafe pw-return ignored -> home');
  ok(A.returnTarget({ documentElement: { getAttribute: () => 'en' } }, { sessionStorage: { getItem: () => JSON.stringify({ p: '/#pricing', t: 0 }) } }, 31 * 60_000) === '/', 'old pw-return ignored');

  // ---- wrong / expired code
  const wrong = { verifyOtp: { data: {}, error: { status: 403, code: 'otp_expired', message: 'Token has expired or is invalid' } }, signUp: { data: { session: null, user: { identities: [{}] } }, error: null } };
  S = setup({ answers: wrong });
  fillSignup(S.$); await S.h.signup();
  S.$.codeInput.value = '111111';
  ok(await S.h.onCodeInput() === 'wrong' && S.$.codeMessage.textContent === 'This code is not correct. Check the email and try again.' && S.$.codeMessage.attrs['data-kind'] === 'error', 'wrong code: clear message');
  ok(S.$.codeInput.selected, 'wrong code: input selected for retyping');
  const nVerify = S.client.calls.filter(c => c[0] === 'verifyOtp').length;
  S.$.codeInput.value = '111111'; await S.h.onCodeInput();
  ok(S.client.calls.filter(c => c[0] === 'verifyOtp').length === nVerify, 'the same wrong code is not auto-submitted again');
  S.t.now += 16 * 60_000;
  ok(await S.h.verify() === 'expired' && S.$.codeMessage.textContent === 'This code has expired. Send a new code.', 'code older than 15 min: "expired" message');
  S.$.codeInput.value = '12';
  ok(await S.h.verify() === 'short' && S.$.codeMessage.textContent === 'Enter the 6-digit code.', 'Verify with fewer than 6 digits: message, no request');

  for (const [ans, kind, text] of [
    [{ status: 429, code: 'over_request_rate_limit', message: 'rate limit' }, 'rate_limit', 'Too many attempts. Wait a few minutes, then try again.'],
    [{ name: 'AuthRetryableFetchError', message: 'Failed to fetch' }, 'network', 'Connection problem. Check your internet and try again.']]) {
    S = setup({ answers: { verifyOtp: { error: ans }, signUp: { data: { session: null, user: { identities: [{}] } }, error: null } } });
    fillSignup(S.$); await S.h.signup();
    S.$.codeInput.value = '222222';
    ok(await S.h.onCodeInput() === kind && S.$.codeMessage.textContent === text, `verify ${kind}: "${text}"`);
  }
  S = setup({ answers: { verifyOtp: async () => { throw new Error('offline'); }, signUp: { data: { session: null, user: { identities: [{}] } }, error: null } } });
  fillSignup(S.$); await S.h.signup(); S.$.codeInput.value = '333333';
  ok(await S.h.onCodeInput() === 'network', 'verify throws -> network message');

  // ---- resend
  S = setup({ answers: { signUp: { data: { session: null, user: { identities: [{}] } }, error: null } } });
  fillSignup(S.$); await S.h.signup(); S.win.tick(60);
  ok(await S.h.resend() === 'sent', 'resend after the countdown');
  const rs = S.client.calls.find(c => c[0] === 'resend')[1];
  ok(rs.type === 'signup' && rs.email === 'eve@example.com' && !('options' in rs), 'resend({type: signup, email}), no captchaToken while off');
  ok(S.$.codeMessage.textContent.startsWith('A new code is on its way') && S.$.resendCode.disabled && S.$.resendCode.textContent === 'Resend code (60 s)', 'resend: success message, countdown again');
  S = setup({ answers: { resend: { error: { status: 429, message: 'Email rate limit exceeded' } }, signUp: { data: { session: null, user: { identities: [{}] } }, error: null } } });
  fillSignup(S.$); await S.h.signup(); S.win.tick(60);
  ok(await S.h.resend() === 'rate_limit' && S.$.codeMessage.textContent.startsWith('Too many attempts') && S.$.resendCode.disabled, 'resend rate limit: message + wait again');

  // ---- change email
  ok(S.h.changeEmail() === 'signup' && visible(S.$).join() === 'panelSignup' && S.$.signupEmail.value === 'eve@example.com', '"Use a different email" -> sign up form with the email');

  // ---- already registered
  S = setup({ answers: { signUp: { data: {}, error: { status: 422, code: 'user_already_exists', message: 'User already registered' } } } });
  fillSignup(S.$);
  ok(await S.h.signup() === 'already-registered' && visible(S.$).join() === 'panelLogin' && S.$.loginEmail.value === 'eve@example.com', 'already registered -> log in with the email filled');
  ok(S.$.loginMessage.textContent.startsWith('This email already has an account'), 'already registered: message');
  S = setup({ answers: { signUp: { data: { session: null, user: { id: 'fake', identities: [] } }, error: null } } });
  fillSignup(S.$);
  ok(await S.h.signup() === 'already-registered', 'Confirm email ON + confirmed address (no identities) -> log in, not a code that never comes');

  // ---- sign up validation
  S = setup(); fillSignup(S.$, { agree: false });
  ok(await S.h.signup() === 'agree' && S.client.calls.length === 0 && S.$.signupMessage.textContent === 'Please accept the Terms and Privacy Policy.', 'Terms not accepted: message, no request');
  S = setup(); fillSignup(S.$, { pw: 'short' });
  ok(await S.h.signup() === 'invalid' && S.$.signupPasswordField.classes.has('has-error') && S.client.calls.length === 0, 'short password: field error, no request');
  for (const [pw, why] of [['abcdefgh', 'letters only'], ['12345678', 'digits only'], ['abc123', 'too short'], ['éééééé12', 'no a-z letter']]) {
    S = setup(); fillSignup(S.$, { pw });
    ok(await S.h.signup() === 'invalid' && S.$.signupPasswordField.classes.has('has-error') && S.client.calls.length === 0, `password rule in the browser: ${why} refused, no request`);
  }
  ok(A.passwordOk('abc12345') && A.passwordOk('ABCDEFG1') && A.passwordOk('pass word 1') && !A.passwordOk('') && !A.passwordOk(null), 'passwordOk: 8+ with a letter and a digit');
  S = setup({ answers: { signUp: { error: { status: 422, code: 'weak_password', message: 'Password should contain at least one character of each: abcdefghijklmnopqrstuvwxyz...' } } } }); fillSignup(S.$);
  ok(await S.h.signup() === 'weak_password' && S.$.signupMessage.textContent === 'Use at least 8 characters, with letters and digits.' && S.$.signupPasswordField.classes.has('has-error'), 'weak password from Supabase: clear message + field marked');
  S = setup({ lang: 'ar', answers: { signUp: { error: { code: 'weak_password' } } } }); fillSignup(S.$);
  await S.h.signup();
  ok(S.$.signupMessage.textContent === 'استخدم 8 أحرف على الأقل، تتضمن حروفًا وأرقامًا.', 'ar: weak password message');

  // ---- log in
  S = setup({ answers: { signInWithPassword: { data: { session: {} }, error: null } } });
  S.$.loginEmail.value = ' Eve@Example.com'; S.$.loginPassword.value = 'pw';
  ok(await S.h.login() === 'signed-in' && S.win.location.replaced === '/', 'log in -> home');
  const li = S.client.calls[0][1];
  ok(li.email === 'eve@example.com' && li.password === 'pw' && !('options' in li), 'signInWithPassword without captchaToken while off');
  S = setup({ answers: { signInWithPassword: { error: { status: 400, code: 'invalid_credentials', message: 'Invalid login credentials' } } } });
  S.$.loginEmail.value = 'eve@example.com'; S.$.loginPassword.value = 'bad';
  ok(await S.h.login() === 'wrong' && S.$.loginMessage.textContent === 'Wrong email or password.', 'wrong password: message (no alert)');
  S = setup({ answers: { signInWithPassword: { error: { status: 400, code: 'email_not_confirmed', message: 'Email not confirmed' } } } });
  S.$.loginEmail.value = 'eve@example.com'; S.$.loginPassword.value = 'pw';
  ok(await S.h.login() === 'code' && visible(S.$).join() === 'panelCode' && S.$.codeEmail.textContent === 'eve@example.com', 'email not confirmed -> code step for that email');
  ok(S.$.codeMessage.textContent.startsWith('Your email is not confirmed yet') && !S.$.resendCode.disabled, 'not confirmed: explanation + Resend code available at once');
  S.$.codeInput.value = '999999';
  S.client.auth.verifyOtp = async () => ({ error: { code: 'otp_expired', message: 'Token has expired or is invalid' } });
  ok(await S.h.verify() === 'wrong' && S.$.codeMessage.textContent === 'This code is wrong or has expired. Check it, or send a new code.', 'unknown send time: "wrong or expired" message');

  // ---- French / Arabic messages
  S = setup({ lang: 'fr', answers: { signInWithPassword: { error: { code: 'invalid_credentials' } } } });
  S.$.loginEmail.value = 'a@b.co'; S.$.loginPassword.value = 'x'; await S.h.login();
  ok(S.$.loginMessage.textContent === 'E-mail ou mot de passe incorrect.', 'fr: wrong password message');
  S = setup({ lang: 'ar', answers: { signUp: { data: { session: null, user: { identities: [{}] } }, error: null } } });
  fillSignup(S.$); await S.h.signup();
  ok(S.$.resendCode.textContent === 'إعادة إرسال الرمز (60 ث)', 'ar: resend countdown');
  ok(S.$.authSwitch.hidden, 'ar: switch hidden on the code step');
  S.h.setTab('login');
  ok(S.$.authSwitch.textContent === 'ليس لديك حساب؟ إنشاء حساب', 'ar: "Don\'t have an account? Sign up"');
  S = setup({ search: '?tab=signup' });
  ok(visible(S.$).join() === 'panelSignup', '?tab=signup opens the sign up tab');

  // ---- every text used by auth.js exists in fr and ar
  const src = read('auth.js');
  const keys = [...new Set([...src.matchAll(/\bt\('((?:[^'\\]|\\.)*)'\)/g), ...src.matchAll(/\bt\("([^"]*)"\)/g)].map(m => m[1].replace(/\\'/g, "'")))];
  const missing = keys.filter(k => !A.I18N.fr[k] || !A.I18N.ar[k]);
  ok(keys.length > 25 && missing.length === 0, `i18n: ${keys.length} texts, all in fr and ar` + (missing.length ? ' missing: ' + missing.join(' | ') : ''));

  // ---- Turnstile
  ok(A.TURNSTILE_SITE_KEY === '0x4AAAAAAGIi-TmOWlcKHDZg', 'Turnstile site key set (public key)');
  ok(!/0x4[A-Za-z0-9_-]*(secret|Secret)|TURNSTILE_SECRET/.test(read('auth.js')), 'no Turnstile secret in auth.js');
  const noScript = { head: { appendChild() { throw new Error('should not load'); } }, querySelectorAll: () => [el('x')] };
  ok(A.createCaptcha(noScript, {}, '').enabled === false, 'Turnstile OFF with an empty key: nothing loaded');
  const noSlots = { head: { appendChild() { throw new Error('should not load'); } }, querySelectorAll: () => [] };
  ok(A.createCaptcha(noSlots, {}).enabled === false, 'page without a Turnstile slot (reset password): script not loaded');
  {
    const appended = [];
    const d0 = { head: { appendChild: x => appended.push(x) }, createElement: () => ({}), querySelectorAll: () => [el('x')], documentElement: { getAttribute: () => 'en' } };
    const w0 = {};
    const c0 = A.createCaptcha(d0, w0);
    c0.ensure('login');
    const drawn = [];
    w0.turnstile = { render: (box, o) => { drawn.push(box.id); return 1; } };
    d0.querySelectorAll = () => [Object.assign(el('slot-login'), { attrs: { 'data-captcha': 'login' } })];
    w0.onPwTurnstileLoad();
    ok(drawn.join() === 'slot-login', 'script loads after the panel was shown: the widget is drawn on load');
    ok(c0.enabled && appended[0].src === 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit&onload=onPwTurnstileLoad' && typeof w0.onPwTurnstileLoad === 'function', 'default key: Turnstile script loaded (explicit render)');
  }
  {
    const boxes = { login: el('cl'), signup: el('cs'), code: el('cc'), forgot: el('cf') };
    Object.entries(boxes).forEach(([k, b]) => { b.attrs['data-captcha'] = k; b.hidden = true; });
    let rendered = {}; const resets = []; const removed = [];
    const win = { turnstile: { render(box, o) { rendered[box.attrs['data-captcha']] = o; return box.attrs['data-captcha']; }, reset(w) { resets.push(w); }, remove(w) { removed.push(w); } } };
    const htmlAttrs = { lang: 'fr' };
    const d = { querySelectorAll: () => Object.values(boxes), documentElement: { getAttribute: k => htmlAttrs[k] ?? null } };
    const cap = A.createCaptcha(d, win, '0x4AAAAAAA-test');
    ok(cap.enabled && Object.keys(rendered).length === 0 && boxes.signup.hidden, 'no widget drawn before its panel is shown');
    {
      const dl = makeDoc(); A.initLogin(dl, makeWin(), makeClient(), { captcha: cap });
      ok(Object.keys(rendered).join() === 'login', 'login page opens: only the log in widget is drawn');
      const h0 = A.initLogin(dl, makeWin(), makeClient(), { captcha: cap });
      h0.setTab('signup');
      ok(Object.keys(rendered).join() === 'login,signup', 'Sign up tab: its widget drawn when shown');
      h0.openCode('a@b.co', {});
      ok('code' in rendered, 'code step: its widget drawn when shown (for Resend code)');
      h0.setTab('login');
      ok(Object.keys(rendered).length === 3, 'switching back does not draw a second widget');
    }
    cap.ensure('forgot');
    ok(cap.enabled && Object.keys(rendered).length === 4 && rendered.login.sitekey === '0x4AAAAAAA-test' && rendered.login.language === 'fr' && !boxes.login.hidden, 'Turnstile ON: a widget in each slot (login, sign up, code, forgot)');
    ok(rendered.login.theme === 'light' && rendered.login.size === 'flexible' && rendered.login['refresh-expired'] === 'auto', 'widget: site theme (light), flexible width for phones, auto refresh of expired tokens');
    rendered.login.callback('old'); rendered.login['expired-callback']();
    ok(cap.token('login') === null, 'expired token (300 s) is never sent');
    rendered.login.callback('old2'); rendered.login['timeout-callback']();
    ok(cap.token('login') === null, 'timed-out challenge: no token');
    // log in blocked until solved, then token passed and widget reset
    const doc = makeDoc(); const w2 = makeWin(); const client = makeClient({ signInWithPassword: { data: {}, error: null } });
    const h = A.initLogin(doc, w2, client, { captcha: cap });
    doc.ids.loginEmail.value = 'a@b.co'; doc.ids.loginPassword.value = 'x';
    ok(await h.login() === 'captcha' && client.calls.length === 0, 'Turnstile ON: log in waits for the security check');
    rendered.login.callback('tok-login');
    await h.login();
    ok(client.calls[0][1].options.captchaToken === 'tok-login' && resets.includes('login') && cap.token('login') === null, 'Turnstile ON: captchaToken sent to signInWithPassword, then reset');
    rendered.signup.callback('tok-signup');
    const c2 = makeClient({ signUp: { data: { session: null, user: { identities: [{}] } }, error: null } });
    const doc2 = makeDoc(); const h2 = A.initLogin(doc2, makeWin(), c2, { captcha: cap });
    fillSignup(doc2.ids); await h2.signup();
    ok(c2.calls[0][1].options.captchaToken === 'tok-signup' && c2.calls[0][1].options.data.full_name === 'Eve', 'Turnstile ON: captchaToken sent to signUp');
    rendered.code.callback('tok-code');
    h2.state.left = 0;
    await h2.resend();
    ok(c2.calls.find(c => c[0] === 'resend')[1].options.captchaToken === 'tok-code', 'Turnstile ON: captchaToken sent to resend');
    rendered.forgot.callback('tok-forgot');
    const fdoc = { getElementById: id => ({ forgotForm: el('f'), email: Object.assign(el('e'), { value: 'Eve@Example.com' }), emailField: el('ef'), submitBtn: el('b'), formBanner: el('fb') })[id], documentElement: { getAttribute: () => 'en' } };
    const fc = makeClient();
    const fh = A.initForgot(fdoc, {}, fc, { captcha: cap });
    await fh.submit();
    const fr = fc.calls[0];
    ok(fr[0] === 'resetPasswordForEmail' && fr[1] === 'eve@example.com' && fr[2].captchaToken === 'tok-forgot', 'Turnstile ON: captchaToken sent to resetPasswordForEmail');
    ok(resets.filter(x => x === 'forgot').length === 1 && cap.token('forgot') === null, 'forgot: widget reset after the call');

    // a fresh token for every call, also after an error
    resets.length = 0;
    const c3 = makeClient({ signInWithPassword: { error: { code: 'invalid_credentials' } } });
    const doc3 = makeDoc(); const h3 = A.initLogin(doc3, makeWin(), c3, { captcha: cap });
    doc3.ids.loginEmail.value = 'a@b.co'; doc3.ids.loginPassword.value = 'x';
    rendered.login.callback('tok-1'); await h3.login();
    ok(resets.includes('login') && cap.token('login') === null, 'wrong password: widget reset after the error');
    ok(await h3.login() === 'captcha' && c3.calls.length === 1, 'second try waits for a NEW token (the used one is not resent)');
    rendered.login.callback('tok-2'); await h3.login();
    ok(c3.calls[1][1].options.captchaToken === 'tok-2' && c3.calls[0][1].options.captchaToken === 'tok-1', 'each call used its own token');
    const c4 = makeClient({ signUp: { error: { status: 500, message: 'boom' } } });
    const doc4 = makeDoc(); const h4 = A.initLogin(doc4, makeWin(), c4, { captcha: cap });
    fillSignup(doc4.ids); rendered.signup.callback('tok-s'); await h4.signup();
    ok(cap.token('signup') === null && resets.includes('signup'), 'sign up error: widget reset');
    const c5 = makeClient({ resend: { error: { status: 429, message: 'rate limit' } } });
    const doc5 = makeDoc(); const h5 = A.initLogin(doc5, makeWin(), c5, { captcha: cap });
    h5.openCode('a@b.co', {}); rendered.code.callback('tok-c'); await h5.resend();
    ok(cap.token('code') === null && resets.includes('code'), 'resend error: widget reset');
    const c6 = makeClient({ signInWithPassword: async () => { throw new Error('offline'); } });
    const doc6 = makeDoc(); const h6 = A.initLogin(doc6, makeWin(), c6, { captcha: cap });
    doc6.ids.loginEmail.value = 'a@b.co'; doc6.ids.loginPassword.value = 'x'; rendered.login.callback('tok-n');
    ok(await h6.login() === 'network' && cap.token('login') === null, 'network error: widget reset');

    // Google is not affected (no token needed, none sent)
    const c7 = makeClient(); const doc7 = makeDoc(); const h7 = A.initLogin(doc7, makeWin(), c7, { captcha: cap });
    ok(cap.token('login') === null && await h7.google() === 'redirecting' && !('captchaToken' in c7.calls[0][1].options), 'Google sign-in works without a Turnstile token');

    // dark mode, Arabic, theme switch
    htmlAttrs['data-theme'] = 'dark'; htmlAttrs.lang = 'ar'; rendered = {};
    cap.rerender();
    ok(removed.length === 4 && rendered.login.theme === 'dark' && rendered.code.language === 'ar' && cap.token('login') === null, 'theme toggle: widgets drawn again in dark, Arabic page language');
  }
  {
    // theme toggle on the page calls the redraw
    const toggle = el('themeToggle'); let redrawn = 0;
    const d = { documentElement: Object.assign(el('html'), {}), getElementById: id => (id === 'themeToggle' ? toggle : null), querySelectorAll: () => [] };
    A.wireChrome(d, { localStorage: { setItem() {} } }, k => k, () => { redrawn++; });
    await toggle.fire('click');
    ok(redrawn === 1 && d.documentElement.attrs['data-theme'] === 'dark', 'theme toggle -> Turnstile redrawn');
  }

  // ---- forgot password (Turnstile off)
  {
    const els = { forgotForm: el('f'), email: Object.assign(el('e'), { value: 'Eve@Example.com' }), emailField: el('ef'), submitBtn: el('b'), formBanner: el('fb') };
    const fdoc = { getElementById: id => els[id], documentElement: { getAttribute: () => 'en' } };
    const fc = makeClient();
    const fh = A.initForgot(fdoc, {}, fc, { captcha: OFF });
    ok(await fh.submit() === 'sent', 'forgot password: sent');
    ok(fc.calls[0][2].redirectTo === 'https://pdf2wordly.com/reset-password.html' && !('captchaToken' in fc.calls[0][2]), 'forgot password: link goes to https://pdf2wordly.com/reset-password.html, no captchaToken');
    for (const l of ['fr', 'ar']) {
      const e3 = { ...els, email: Object.assign(el('e'), { value: 'eve@example.com' }), formBanner: el('fb3') };
      const fc3 = makeClient();
      await A.initForgot({ getElementById: id => e3[id], documentElement: { getAttribute: () => l } }, {}, fc3, { captcha: OFF }).submit();
      ok(fc3.calls[0][2].redirectTo === `https://pdf2wordly.com/${l}/reset-password.html`, `forgot password (${l}): link to /${l}/reset-password.html`);
    }
    ok(fs.existsSync(path.join(SITE, 'reset-password.html')) && read('reset-password.html').includes('id="resetForm"'), 'reset-password.html exists (form handled by auth.js initReset)');
    ok(els.formBanner.textContent.startsWith('If an account exists'), 'forgot password: same message for any address');
    const fc2 = makeClient({ resetPasswordForEmail: { error: { status: 429, message: 'rate limit' } } });
    const els2 = { ...els, email: Object.assign(el('e'), { value: 'eve@example.com' }), formBanner: el('fb2') };
    const fh2 = A.initForgot({ getElementById: id => els2[id], documentElement: { getAttribute: () => 'en' } }, {}, fc2, { captcha: OFF });
    ok(await fh2.submit() === 'rate_limit' && els2.formBanner.textContent.startsWith('Too many attempts'), 'forgot password: rate limit message');
  }

  // ---- reset password
  {
    const mk = (lang = 'en') => {
      const ids = {}; ['resetForm', 'newPassword', 'confirmPassword', 'submitBtn', 'formBanner', 'passwordField', 'confirmField', 'resetState', 'invalidState'].forEach(i => { ids[i] = el(i); });
      ids.invalidState.hidden = true;
      return { ids, getElementById: i => ids[i] || null, documentElement: { getAttribute: () => lang } };
    };
    const later = [];
    const w = () => ({ location: { hash: '', search: '', href: null }, setTimeout: (f, ms) => later.push([f, ms]) });
    let d = mk(); let win = w(); let c = makeClient({ updateUser: null });
    c.auth.updateUser = async a => { c.calls.push(['updateUser', a]); return { data: {}, error: null }; };
    c.auth.getSession = async () => ({ data: { session: { access_token: 't' } } });
    let h = A.initReset(d, win, c, {});
    for (const [pw, why] of [['abcdefgh', 'letters only'], ['12345678', 'digits only'], ['ab1', 'too short']]) {
      d.ids.newPassword.value = pw; d.ids.confirmPassword.value = pw;
      ok(await h.submit() === 'invalid' && d.ids.passwordField.classes.has('has-error') && !c.calls.length, `reset: ${why} refused in the browser`);
    }
    d.ids.newPassword.value = 'newpass12'; d.ids.confirmPassword.value = 'newpass13';
    ok(await h.submit() === 'invalid' && d.ids.confirmField.classes.has('has-error') && !d.ids.passwordField.classes.has('has-error') && !c.calls.length, 'reset: passwords that do not match refused');
    d.ids.confirmPassword.value = 'newpass12';
    ok(await h.submit() === 'updated' && c.calls[0][1].password === 'newpass12' && d.ids.formBanner.textContent.startsWith('Your password has been updated'), 'reset: updateUser with the new password');
    later.filter(([, ms]) => ms === 1500).forEach(([f]) => f());
    ok(win.location.href === '/', 'reset: then home');
    // Supabase errors
    for (const [err, kind, text] of [
      [{ status: 422, code: 'weak_password', message: 'Password should contain...' }, 'weak_password', 'Use at least 8 characters, with letters and digits.'],
      [{ status: 422, code: 'same_password', message: 'New password should be different from the old password.' }, 'same_password', 'Choose a password different from your current one.'],
      [{ status: 429, message: 'rate limit' }, 'rate_limit', 'Too many attempts. Wait a few minutes, then try again.']]) {
      d = mk(); c = makeClient(); c.auth.updateUser = async () => ({ error: err });
      h = A.initReset(d, w(), c, { checkLink: false });
      d.ids.newPassword.value = 'newpass12'; d.ids.confirmPassword.value = 'newpass12';
      ok(await h.submit() === kind && d.ids.formBanner.textContent === text, `reset: Supabase ${kind} -> "${text}"`);
    }
    d = mk('fr'); c = makeClient(); c.auth.updateUser = async () => ({ error: { code: 'weak_password' } });
    h = A.initReset(d, w(), c, { checkLink: false });
    d.ids.newPassword.value = 'newpass12'; d.ids.confirmPassword.value = 'newpass12';
    await h.submit();
    ok(d.ids.formBanner.textContent === 'Utilisez au moins 8 caractères, avec des lettres et des chiffres.', 'reset (fr): weak password message');
    // invalid link
    d = mk(); c = makeClient(); c.auth.getSession = async () => ({ data: { session: null } });
    h = A.initReset(d, w(), c, { checkLink: false });
    ok(await h.checkLink() === 'invalid' && d.ids.resetState.hidden && !d.ids.invalidState.hidden, 'reset: no session and no recovery data -> "This link isn\'t valid"');
    d = mk(); const w2 = w(); w2.location.hash = '#access_token=x&type=recovery';
    h = A.initReset(d, w2, c, { checkLink: false });
    ok(await h.checkLink() === 'ok', 'reset: recovery link accepted');
    d = mk('ar'); c = makeClient(); c.auth.updateUser = async () => ({ data: {}, error: null }); later.length = 0;
    const w3 = w(); h = A.initReset(d, w3, c, { checkLink: false });
    d.ids.newPassword.value = 'newpass12'; d.ids.confirmPassword.value = 'newpass12'; await h.submit();
    later.forEach(([f]) => f());
    ok(w3.location.href === '/ar/', 'reset (ar): then /ar/');
  }

  // ---- pages
  for (const [f, lang, pre] of [['login.html', 'en', ''], ['fr/login.html', 'fr', '../'], ['ar/login.html', 'ar', '../']]) {
    const s = read(f);
    ok(s.includes(`<html lang="${lang}"`) && (lang !== 'ar' || s.includes('<html lang="ar" dir="rtl">')), `${f}: lang${lang === 'ar' ? ' + rtl' : ''}`);
    for (const panel of ['panelLogin', 'panelSignup']) {
      const part = s.slice(s.indexOf(`id="${panel}"`));
      const firstButton = part.search(/<button\b/);
      ok(part.slice(firstButton, firstButton + 40).includes('class="btn-google"') && firstButton < part.indexOf('<form'), `${f} ${panel}: Google is the first button, before the form`);
    }
    ok((s.match(/class="btn-google"/g) || []).length === 2, `${f}: Google on both tabs`);
    ok(/<input type="text" id="codeInput" class="code-input" inputmode="numeric" autocomplete="one-time-code"/.test(s), `${f}: code input (numeric, one-time-code)`);
    ok(['loginForm', 'signupForm', 'codeForm', 'resendCode', 'changeEmail', 'codeMessage', 'signupMessage', 'loginMessage', 'signupAgree', 'loginSubmit', 'signupSubmit']
      .every(id => s.includes(`id="${id}"`)), `${f}: all elements for auth.js`);
    ok(['login', 'signup', 'code'].every(n => s.includes(`data-captcha="${n}" hidden`)), `${f}: Turnstile slots (hidden)`);
    ok(s.includes(`<script src="${pre}auth.js" defer></script>`) && !/signInWithPassword|alert\(/.test(s), `${f}: logic in auth.js, no alert()`);
    ok(s.includes('href="/login.html"') && s.includes('href="/fr/login.html"') && s.includes('href="/ar/login.html"'), `${f}: language switcher -> login pages`);
    ok(s.includes('noindex'), `${f}: noindex kept`);
    ok(!/remember/i.test(s), `${f}: no "Remember me"`);
    ok(s.includes(`href="forgot-password.html" class="link-inline"`), `${f}: "Forgot password?" -> same-language page`);
    const hint = { en: 'At least 8 characters, with letters and digits.', fr: 'Au moins 8 caractères, avec des lettres et des chiffres.', ar: '8 أحرف على الأقل، تتضمن حروفًا وأرقامًا.' }[lang];
    ok(s.includes(`<p class="field-hint">${hint}</p>`), `${f}: password rule under the field`);
  }
  for (const [f, lang, pre] of [['forgot-password.html', 'en', ''], ['fr/forgot-password.html', 'fr', '../'], ['ar/forgot-password.html', 'ar', '../'],
                                ['reset-password.html', 'en', ''], ['fr/reset-password.html', 'fr', '../'], ['ar/reset-password.html', 'ar', '../']]) {
    const s = read(f);
    const page = f.split('/').pop();
    ok(s.includes(`<html lang="${lang}"`) && (lang !== 'ar' || s.includes('dir="rtl"')) && s.includes('noindex'), `${f}: lang, noindex`);
    ok(s.includes(`<script src="${pre}auth.js" defer></script>`) && !/updateUser|resetPasswordForEmail|alert\(/.test(s), `${f}: logic in auth.js`);
    ok(['', 'fr/', 'ar/'].every(x => s.includes(`href="/${x}${page}"`)), `${f}: language switcher -> ${page} in each language`);
    if (page === 'reset-password.html') {
      const hint = { en: 'At least 8 characters, with letters and digits.', fr: 'Au moins 8 caractères, avec des lettres et des chiffres.', ar: '8 أحرف على الأقل، تتضمن حروفًا وأرقامًا.' }[lang];
      ok(s.includes(`<p class="field-hint">${hint}</p>`) && ['resetForm', 'newPassword', 'confirmPassword', 'passwordField', 'confirmField', 'invalidState'].every(i => s.includes(`id="${i}"`)), `${f}: password rule + form elements`);
    } else {
      ok(s.includes('data-captcha="forgot" hidden') && s.includes('href="login.html"'), `${f}: Turnstile slot, back to the same-language login`);
    }
  }
  ok(/\.auth-captcha\{display:flex;justify-content:center;width:100%/.test(read('style.css')) && read('style.css').includes('.auth-captcha[hidden]{display:none}'), 'style.css: Turnstile slot centered, hidden until used');
  for (const f of ['login.html', 'fr/login.html', 'ar/login.html']) {
    const sl = read(f);
    ok(['login', 'signup', 'code'].every(n => (sl.match(new RegExp(`data-captcha="${n}"`, 'g')) || []).length === 1), `${f}: one slot each for log in, sign up, code`);
    const code = sl.slice(sl.indexOf('id="panelCode"'));
    ok(code.indexOf('data-captcha="code"') < code.indexOf('id="resendCode"'), `${f}: code slot before "Resend code"`);
  }
  for (const f of ['forgot-password.html', 'fr/forgot-password.html', 'ar/forgot-password.html']) {
    ok((read(f).match(/data-captcha="forgot"/g) || []).length === 1, `${f}: forgot slot`);
  }
  for (const f of ['reset-password.html', 'fr/reset-password.html', 'ar/reset-password.html']) {
    ok(!read(f).includes('data-captcha'), `${f}: no slot (updateUser is not protected by Turnstile)`);
  }
  ok(read('forgot-password.html').includes('data-captcha="forgot" hidden') && read('forgot-password.html').includes('<script src="auth.js" defer></script>'), 'forgot-password.html: Turnstile slot + auth.js');
  for (const f of ['fr/index.html', 'ar/index.html', 'fr/delete-account.html', 'ar/delete-account.html']) {
    ok(!read(f).includes('../login.html') && read(f).includes('href="login.html"'), `${f}: login link -> same-language login page`);
  }
  ok(/function u\(\)\{return"login\.html"\}/.test(read('script.js')), 'script.js: login redirect stays in the page language');

  console.log(`\n${passed} passed`);
})().catch(e => { console.error(e.stack || e.message); process.exit(1); });
