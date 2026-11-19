/*
 * PDF2Wordly sign-in pages: login.html (+ fr/, ar/) and forgot-password.html.
 *
 * - Google first: signInWithOAuth with prompt=select_account, so the Google
 *   account chooser always shows.
 * - Email sign up: signUp, then a "Check your email" step with the 6-digit
 *   code from the "Confirm signup" email (verifyOtp type 'signup'). Paste of
 *   the full code works and 6 digits submit by themselves. "Resend code" has
 *   a 60 s countdown. While "Confirm email" is OFF in Supabase, signUp
 *   returns a session and the user goes straight in.
 * - Log in with an unconfirmed email -> the same code step for that email.
 * - After sign-in: back to the pricing section the user came from
 *   (sessionStorage "pw-return", same allowlist as billing.js), else the home
 *   page in the page language.
 * - Passwords: at least 8 characters with letters and digits (same rule as
 *   Supabase), checked here before calling Supabase (sign up, reset).
 * - Google, the reset link and the end of a reset go back to the page
 *   language (/, /fr/, /ar/).
 * - Cloudflare Turnstile: OFF while TURNSTILE_SITE_KEY is ''. Then no widget
 *   is loaded and no captchaToken is sent.
 *
 * Texts: English keys, French and Arabic in I18N below (same shape as
 * pwI18n in script.js), chosen by <html lang>.
 */
(function () {
  'use strict';

  // ---- Configuration -----------------------------------------------------------
  // Cloudflare Turnstile site key (public). '' = OFF. When set, the secret key
  // goes in Supabase -> Authentication -> Attack Protection (never here).
  var TURNSTILE_SITE_KEY = '';
  var TURNSTILE_SRC = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
  // All must be in Supabase -> Authentication -> URL Configuration -> Redirect URLs.
  var OAUTH_REDIRECT_URLS = {
    en: 'https://pdf2wordly.com/',
    fr: 'https://pdf2wordly.com/fr/',
    ar: 'https://pdf2wordly.com/ar/',
  };
  var RESET_PASSWORD_URLS = {
    en: 'https://pdf2wordly.com/reset-password.html',
    fr: 'https://pdf2wordly.com/fr/reset-password.html',
    ar: 'https://pdf2wordly.com/ar/reset-password.html',
  };
  // -------------------------------------------------------------------------------

  var CODE_LENGTH = 6;
  var RESEND_SECONDS = 60;
  var CODE_TTL_MS = 15 * 60 * 1000;
  var RETURN_KEY = 'pw-return';
  var RETURN_MAX_AGE_MS = 30 * 60 * 1000;
  var ALLOWED_RETURNS = ['/#pricing', '/fr/#pricing', '/ar/#pricing'];
  var HOME = { en: '/', fr: '/fr/', ar: '/ar/' };
  var EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
  var PASSWORD_MIN = 8;

  var I18N = {
    fr: {
      'Log in': 'Connexion',
      'Sign up': 'Inscription',
      "Don't have an account?": 'Pas encore de compte ?',
      'Already have an account?': 'Vous avez déjà un compte ?',
      'Show password': 'Afficher le mot de passe',
      'Hide password': 'Masquer le mot de passe',
      'Switch to dark theme': 'Passer au thème sombre',
      'Switch to light theme': 'Passer au thème clair',
      'Open menu': 'Ouvrir le menu',
      'Close menu': 'Fermer le menu',
      'Wrong email or password.': 'E-mail ou mot de passe incorrect.',
      'This email already has an account. Enter your password below to log in.': 'Cette adresse e-mail a déjà un compte. Saisissez votre mot de passe ci-dessous pour vous connecter.',
      'Please accept the Terms and Privacy Policy.': 'Veuillez accepter les conditions d’utilisation et la politique de confidentialité.',
      'Use at least 8 characters, with letters and digits.': 'Utilisez au moins 8 caractères, avec des lettres et des chiffres.',
      'Choose a password different from your current one.': 'Choisissez un mot de passe différent de l’actuel.',
      'Sending…': 'Envoi…',
      'Send reset link': 'Envoyer le lien',
      'If an account exists for that email, a reset link is on its way. Check your inbox (and spam folder).': 'Si un compte existe pour cette adresse, un lien de réinitialisation est en route. Vérifiez votre boîte de réception (et les spams).',
      'Updating…': 'Mise à jour…',
      'Update password': 'Mettre à jour le mot de passe',
      'Your password has been updated. Redirecting you now…': 'Votre mot de passe a été mis à jour. Redirection en cours…',
      'Too many attempts. Wait a few minutes, then try again.': 'Trop de tentatives. Patientez quelques minutes, puis réessayez.',
      'Connection problem. Check your internet and try again.': 'Problème de connexion. Vérifiez votre connexion Internet et réessayez.',
      'Something went wrong. Please try again later.': 'Une erreur est survenue. Veuillez réessayer plus tard.',
      'Please complete the security check.': 'Veuillez effectuer la vérification de sécurité.',
      'Your email is not confirmed yet. Enter the 6-digit code we sent you, or send a new one.': 'Votre adresse e-mail n’est pas encore confirmée. Saisissez le code à 6 chiffres que nous vous avons envoyé, ou demandez-en un nouveau.',
      'Enter the 6-digit code.': 'Saisissez le code à 6 chiffres.',
      'This code is not correct. Check the email and try again.': 'Ce code n’est pas correct. Vérifiez l’e-mail et réessayez.',
      'This code has expired. Send a new code.': 'Ce code a expiré. Demandez un nouveau code.',
      'This code is wrong or has expired. Check it, or send a new code.': 'Ce code est incorrect ou a expiré. Vérifiez-le ou demandez un nouveau code.',
      'A new code is on its way. Check your inbox (and spam folder).': 'Un nouveau code est en route. Vérifiez votre boîte de réception (et les spams).',
      'Resend code': 'Renvoyer le code',
      'Resend code ({s} s)': 'Renvoyer le code ({s} s)',
      'Verify': 'Vérifier',
      'Verifying…': 'Vérification…',
      'Your email is confirmed. Log in to continue.': 'Votre adresse e-mail est confirmée. Connectez-vous pour continuer.',
      'Logging in…': 'Connexion…',
      'Creating account…': 'Création du compte…',
      'Create account': 'Créer un compte',
    },
    ar: {
      'Log in': 'تسجيل الدخول',
      'Sign up': 'إنشاء حساب',
      "Don't have an account?": 'ليس لديك حساب؟',
      'Already have an account?': 'لديك حساب بالفعل؟',
      'Show password': 'إظهار كلمة المرور',
      'Hide password': 'إخفاء كلمة المرور',
      'Switch to dark theme': 'التبديل إلى الوضع الليلي',
      'Switch to light theme': 'التبديل إلى الوضع النهاري',
      'Open menu': 'فتح القائمة',
      'Close menu': 'إغلاق القائمة',
      'Wrong email or password.': 'البريد الإلكتروني أو كلمة المرور غير صحيحة.',
      'This email already has an account. Enter your password below to log in.': 'هذا البريد الإلكتروني لديه حساب بالفعل. أدخل كلمة المرور أدناه لتسجيل الدخول.',
      'Please accept the Terms and Privacy Policy.': 'يرجى قبول شروط الاستخدام وسياسة الخصوصية.',
      'Use at least 8 characters, with letters and digits.': 'استخدم 8 أحرف على الأقل، تتضمن حروفًا وأرقامًا.',
      'Choose a password different from your current one.': 'اختر كلمة مرور مختلفة عن كلمة المرور الحالية.',
      'Sending…': 'جارٍ الإرسال…',
      'Send reset link': 'إرسال رابط إعادة التعيين',
      'If an account exists for that email, a reset link is on its way. Check your inbox (and spam folder).': 'إذا كان هناك حساب بهذا البريد الإلكتروني، فإن رابط إعادة التعيين في الطريق إليك. تحقّق من بريدك الوارد (ومجلد الرسائل غير المرغوب فيها).',
      'Updating…': 'جارٍ التحديث…',
      'Update password': 'تحديث كلمة المرور',
      'Your password has been updated. Redirecting you now…': 'تم تحديث كلمة المرور. جارٍ إعادة توجيهك…',
      'Too many attempts. Wait a few minutes, then try again.': 'محاولات كثيرة جدًا. انتظر بضع دقائق ثم حاول مرة أخرى.',
      'Connection problem. Check your internet and try again.': 'مشكلة في الاتصال. تحقّق من اتصالك بالإنترنت وحاول مرة أخرى.',
      'Something went wrong. Please try again later.': 'حدث خطأ ما. يرجى المحاولة لاحقًا.',
      'Please complete the security check.': 'يرجى إكمال التحقق الأمني.',
      'Your email is not confirmed yet. Enter the 6-digit code we sent you, or send a new one.': 'لم يتم تأكيد بريدك الإلكتروني بعد. أدخل الرمز المكوّن من 6 أرقام الذي أرسلناه إليك، أو اطلب رمزًا جديدًا.',
      'Enter the 6-digit code.': 'أدخل الرمز المكوّن من 6 أرقام.',
      'This code is not correct. Check the email and try again.': 'هذا الرمز غير صحيح. تحقّق من البريد الإلكتروني وحاول مرة أخرى.',
      'This code has expired. Send a new code.': 'انتهت صلاحية هذا الرمز. اطلب رمزًا جديدًا.',
      'This code is wrong or has expired. Check it, or send a new code.': 'هذا الرمز غير صحيح أو منتهي الصلاحية. تحقّق منه أو اطلب رمزًا جديدًا.',
      'A new code is on its way. Check your inbox (and spam folder).': 'رمز جديد في الطريق إليك. تحقّق من بريدك الوارد (ومجلد الرسائل غير المرغوب فيها).',
      'Resend code': 'إعادة إرسال الرمز',
      'Resend code ({s} s)': 'إعادة إرسال الرمز ({s} ث)',
      'Verify': 'تحقّق',
      'Verifying…': 'جارٍ التحقق…',
      'Your email is confirmed. Log in to continue.': 'تم تأكيد بريدك الإلكتروني. سجّل الدخول للمتابعة.',
      'Logging in…': 'جارٍ تسجيل الدخول…',
      'Creating account…': 'جارٍ إنشاء الحساب…',
      'Create account': 'إنشاء الحساب',
    },
  };

  // ---- Helpers -----------------------------------------------------------------
  function lang(doc) {
    var l = String((doc.documentElement && doc.documentElement.getAttribute('lang')) || 'en').slice(0, 2).toLowerCase();
    return HOME[l] ? l : 'en';
  }

  function translator(doc) {
    var dict = I18N[lang(doc)];
    return function (key) { return (dict && dict[key]) || key; };
  }

  /** Digits only (Arabic-Indic digits accepted), at most 6. */
  function cleanCode(value) {
    return String(value || '')
      .replace(/[٠-٩]/g, function (d) { return String(d.charCodeAt(0) - 0x0660); })
      .replace(/[۰-۹]/g, function (d) { return String(d.charCodeAt(0) - 0x06F0); })
      .replace(/\D/g, '')
      .slice(0, CODE_LENGTH);
  }

  /** Supabase auth error -> one of our cases. */
  function classifyError(error) {
    if (!error) return null;
    var code = String(error.code || '').toLowerCase();
    var msg = String(error.message || '').toLowerCase();
    var status = Number(error.status || 0);
    if (status === 429 || /^over_.*rate_limit$/.test(code) || /rate limit|too many/.test(msg)) return 'rate_limit';
    if (error.name === 'AuthRetryableFetchError' || /failed to fetch|network|load failed|fetch failed/.test(msg)) return 'network';
    if (code === 'captcha_failed' || /captcha/.test(msg)) return 'captcha';
    if (code === 'email_not_confirmed' || /email not confirmed/.test(msg)) return 'not_confirmed';
    if (code === 'otp_expired' || code === 'otp_disabled' || /expired or is invalid|token has expired|invalid otp|otp.*(expired|invalid)/.test(msg)) return 'otp';
    if (code === 'invalid_credentials' || /invalid login credentials/.test(msg)) return 'invalid_credentials';
    if (code === 'user_already_exists' || code === 'email_exists' || /already (registered|exists|been registered)/.test(msg)) return 'already_registered';
    if (code === 'same_password' || /different from the old password|same password/.test(msg)) return 'same_password';
    if (code === 'weak_password' || /password should|weak password|password is known/.test(msg)) return 'weak_password';
    return 'other';
  }

  function commonMessage(kind, t) {
    if (kind === 'rate_limit') return t('Too many attempts. Wait a few minutes, then try again.');
    if (kind === 'network') return t('Connection problem. Check your internet and try again.');
    if (kind === 'captcha') return t('Please complete the security check.');
    return t('Something went wrong. Please try again later.');
  }

  /** Supabase rule: at least 8 characters, with a letter and a digit. */
  function passwordOk(pw) {
    pw = String(pw || '');
    return pw.length >= PASSWORD_MIN && /[A-Za-z]/.test(pw) && /[0-9]/.test(pw);
  }

  /** Where to go after sign-in: the pricing section saved by billing.js, else home. */
  function returnTarget(doc, win, now) {
    try {
      var v = JSON.parse(win.sessionStorage.getItem(RETURN_KEY) || 'null');
      if (v && typeof v.t === 'number' && now - v.t >= 0 && now - v.t <= RETURN_MAX_AGE_MS
          && ALLOWED_RETURNS.indexOf(v.p) !== -1) {
        return v.p; // billing.js clears it on that page
      }
    } catch (e) { /* storage blocked or bad value */ }
    return HOME[lang(doc)];
  }

  /** Google returns to the home page of the page language (pricing return: billing.js). */
  function googleOptions(l) {
    return {
      provider: 'google',
      options: { redirectTo: OAUTH_REDIRECT_URLS[l] || OAUTH_REDIRECT_URLS.en, queryParams: { prompt: 'select_account' } },
    };
  }

  // ---- Cloudflare Turnstile ------------------------------------------------------
  /**
   * OFF (site key ''): enabled=false, token() -> null, nothing is loaded.
   * ON: one widget per [data-captcha="name"] container; token(name) gives the
   * current token, reset(name) after each use (tokens are single-use).
   */
  function createCaptcha(doc, win, siteKey) {
    var key = siteKey === undefined ? TURNSTILE_SITE_KEY : siteKey;
    var off = { enabled: false, token: function () { return null; }, reset: function () {}, options: function () { return undefined; } };
    if (!key) return off;
    var tokens = {};
    var widgets = {};
    function renderAll() {
      var boxes = doc.querySelectorAll('[data-captcha]');
      for (var i = 0; i < boxes.length; i++) {
        (function (box) {
          var name = box.getAttribute('data-captcha');
          box.hidden = false;
          widgets[name] = win.turnstile.render(box, {
            sitekey: key,
            language: lang(doc),
            callback: function (tk) { tokens[name] = tk; },
            'expired-callback': function () { tokens[name] = null; },
            'error-callback': function () { tokens[name] = null; },
          });
        })(boxes[i]);
      }
    }
    if (win.turnstile) renderAll();
    else {
      win.onPwTurnstileLoad = renderAll;
      var s = doc.createElement('script');
      s.src = TURNSTILE_SRC + '&onload=onPwTurnstileLoad';
      s.async = true;
      s.defer = true;
      doc.head.appendChild(s);
    }
    return {
      enabled: true,
      token: function (name) { return tokens[name] || null; },
      reset: function (name) {
        tokens[name] = null;
        try { if (win.turnstile && widgets[name] !== undefined) win.turnstile.reset(widgets[name]); } catch (e) { /* ignore */ }
      },
      /** {captchaToken} for Supabase, or undefined when there is no token. */
      options: function (name) { var tk = tokens[name]; return tk ? { captchaToken: tk } : undefined; },
    };
  }

  // ---- Page chrome: theme, mobile menu, password eye -----------------------------
  function wireChrome(doc, win, t) {
    var root = doc.documentElement;
    var toggle = doc.getElementById('themeToggle');
    function paint() {
      var dark = root.getAttribute('data-theme') === 'dark';
      if (toggle) {
        toggle.setAttribute('aria-pressed', String(dark));
        toggle.setAttribute('aria-label', dark ? t('Switch to light theme') : t('Switch to dark theme'));
      }
    }
    if (toggle) {
      paint();
      toggle.addEventListener('click', function () {
        var dark = root.getAttribute('data-theme') === 'dark';
        if (dark) root.removeAttribute('data-theme'); else root.setAttribute('data-theme', 'dark');
        try { win.localStorage.setItem('pdftoword-theme', dark ? 'light' : 'dark'); } catch (e) { /* blocked */ }
        paint();
      });
    }
    var menuBtn = doc.getElementById('mobileMenuToggle');
    var menu = doc.getElementById('mobileMenu');
    if (menuBtn && menu) {
      menuBtn.addEventListener('click', function () {
        var open = menuBtn.getAttribute('aria-expanded') === 'true';
        menuBtn.setAttribute('aria-expanded', String(!open));
        menuBtn.setAttribute('aria-label', open ? t('Open menu') : t('Close menu'));
        menu.hidden = open;
      });
    }
    var eyes = doc.querySelectorAll('.password-toggle');
    for (var i = 0; i < eyes.length; i++) {
      (function (btn) {
        btn.setAttribute('aria-label', t('Show password'));
        btn.addEventListener('click', function () {
          var input = doc.getElementById(btn.getAttribute('data-target'));
          var visible = btn.getAttribute('data-visible') === 'true';
          if (input) input.type = visible ? 'password' : 'text';
          btn.setAttribute('data-visible', String(!visible));
          btn.setAttribute('aria-label', visible ? t('Show password') : t('Hide password'));
        });
      })(eyes[i]);
    }
  }

  // ---- Login / sign up / code ----------------------------------------------------
  function initLogin(doc, win, client, opts) {
    opts = opts || {};
    var t = translator(doc);
    var now = opts.now || function () { return Date.now(); };
    var captcha = opts.captcha || createCaptcha(doc, win);
    var $ = function (id) { return doc.getElementById(id); };
    var tabs = $('authTabs');
    var tabLogin = $('tabLogin');
    var tabSignup = $('tabSignup');
    var panels = { login: $('panelLogin'), signup: $('panelSignup'), code: $('panelCode') };
    var authSwitch = $('authSwitch');
    var msg = { login: $('loginMessage'), signup: $('signupMessage'), code: $('codeMessage') };
    var codeInput = $('codeInput');
    var resendBtn = $('resendCode');
    var verifyBtn = $('verifyCode');
    var state = { tab: 'login', email: '', sentAt: null, busy: false, lastTried: '', timer: null, left: 0 };

    function show(box, kind, text) {
      if (!box) return;
      box.textContent = text;
      box.setAttribute('data-kind', kind);
      box.hidden = false;
    }
    function hide(box) {
      if (!box) return;
      box.hidden = true;
      box.textContent = '';
    }
    function field(id, ok) {
      var f = $(id);
      if (f && f.classList) f.classList.toggle('has-error', !ok);
      return ok;
    }

    function renderSwitch(tab) {
      if (!authSwitch) return;
      authSwitch.textContent = '';
      if (tab === 'code') { authSwitch.hidden = true; return; }
      authSwitch.hidden = false;
      var other = tab === 'login' ? 'signup' : 'login';
      authSwitch.appendChild(doc.createTextNode((tab === 'login' ? t("Don't have an account?") : t('Already have an account?')) + ' '));
      var b = doc.createElement('button');
      b.type = 'button';
      b.textContent = other === 'signup' ? t('Sign up') : t('Log in');
      b.addEventListener('click', function () { setTab(other); });
      authSwitch.appendChild(b);
    }

    function setTab(tab) {
      state.tab = tab;
      ['login', 'signup', 'code'].forEach(function (k) {
        if (!panels[k]) return;
        panels[k].hidden = k !== tab;
        if (panels[k].classList) panels[k].classList.toggle('is-active', k === tab);
      });
      if (tabs) {
        tabs.hidden = tab === 'code';
        if (tab !== 'code') tabs.setAttribute('data-active', tab);
      }
      if (tabLogin) tabLogin.setAttribute('aria-selected', String(tab === 'login'));
      if (tabSignup) tabSignup.setAttribute('aria-selected', String(tab === 'signup'));
      renderSwitch(tab);
      if (tab !== 'code') stopCountdown();
    }

    function goSignedIn() {
      win.location.replace(returnTarget(doc, win, now()));
      return 'signed-in';
    }

    // -- countdown for "Resend code"
    function stopCountdown() {
      if (state.timer) win.clearInterval(state.timer);
      state.timer = null;
      state.left = 0;
      if (resendBtn) {
        resendBtn.disabled = false;
        resendBtn.textContent = t('Resend code');
      }
    }
    function startCountdown(seconds) {
      stopCountdown();
      state.left = seconds;
      if (!resendBtn) return;
      resendBtn.disabled = true;
      resendBtn.textContent = t('Resend code ({s} s)').replace('{s}', String(state.left));
      state.timer = win.setInterval(function () {
        state.left -= 1;
        if (state.left <= 0) { stopCountdown(); return; }
        resendBtn.textContent = t('Resend code ({s} s)').replace('{s}', String(state.left));
      }, 1000);
    }

    function openCode(email, o) {
      o = o || {};
      state.email = email;
      state.sentAt = o.sentAt || null;
      state.lastTried = '';
      var shown = $('codeEmail');
      if (shown) shown.textContent = email;
      if (codeInput) codeInput.value = '';
      hide(msg.code);
      if (o.notConfirmed) show(msg.code, 'info', t('Your email is not confirmed yet. Enter the 6-digit code we sent you, or send a new one.'));
      setTab('code');
      if (state.sentAt) startCountdown(RESEND_SECONDS); else stopCountdown();
      if (codeInput && codeInput.focus) codeInput.focus();
    }

    function alreadyRegistered(email) {
      setTab('login');
      var le = $('loginEmail');
      var lp = $('loginPassword');
      if (le) le.value = email;
      if (lp) { lp.value = ''; if (lp.focus) lp.focus(); }
      show(msg.login, 'info', t('This email already has an account. Enter your password below to log in.'));
      return 'already-registered';
    }

    function setBusy(btn, busy, busyText, idleText) {
      if (!btn) return;
      btn.disabled = busy;
      btn.textContent = busy ? busyText : idleText;
    }

    // -- Google
    async function google() {
      try {
        var r = await client.auth.signInWithOAuth(googleOptions(lang(doc)));
        if (r && r.error) { show(msg[state.tab] || msg.login, 'error', commonMessage(classifyError(r.error), t)); return 'error'; }
        return 'redirecting';
      } catch (e) {
        show(msg[state.tab] || msg.login, 'error', commonMessage('network', t));
        return 'error';
      }
    }

    // -- Log in
    async function login(ev) {
      if (ev && ev.preventDefault) ev.preventDefault();
      if (state.busy) return 'busy';
      hide(msg.login);
      var email = String(($('loginEmail') || {}).value || '').trim().toLowerCase();
      var password = String(($('loginPassword') || {}).value || '');
      var ok = field('loginEmailField', EMAIL_RE.test(email)) & field('loginPasswordField', password.length > 0);
      if (!ok) return 'invalid';
      if (captcha.enabled && !captcha.token('login')) { show(msg.login, 'error', t('Please complete the security check.')); return 'captcha'; }
      var btn = $('loginSubmit');
      var idle = btn ? btn.textContent : '';
      state.busy = true;
      setBusy(btn, true, t('Logging in…'), idle);
      var creds = { email: email, password: password };
      var copt = captcha.options('login');
      if (copt) creds.options = copt;
      var r;
      try { r = await client.auth.signInWithPassword(creds); } catch (e) { r = { error: { name: 'AuthRetryableFetchError', message: 'Failed to fetch' } }; }
      state.busy = false;
      setBusy(btn, false, '', idle);
      captcha.reset('login');
      var kind = classifyError(r && r.error);
      if (!kind) return goSignedIn();
      if (kind === 'not_confirmed') { openCode(email, { notConfirmed: true }); return 'code'; }
      if (kind === 'invalid_credentials') { show(msg.login, 'error', t('Wrong email or password.')); return 'wrong'; }
      show(msg.login, 'error', commonMessage(kind, t));
      return kind;
    }

    // -- Sign up
    async function signup(ev) {
      if (ev && ev.preventDefault) ev.preventDefault();
      if (state.busy) return 'busy';
      hide(msg.signup);
      var name = String(($('signupName') || {}).value || '').trim();
      var email = String(($('signupEmail') || {}).value || '').trim().toLowerCase();
      var password = String(($('signupPassword') || {}).value || '');
      var ok = field('signupNameField', name.length > 0)
        & field('signupEmailField', EMAIL_RE.test(email))
        & field('signupPasswordField', passwordOk(password));
      if (!ok) return 'invalid';
      var agree = $('signupAgree');
      if (agree && !agree.checked) { show(msg.signup, 'error', t('Please accept the Terms and Privacy Policy.')); return 'agree'; }
      if (captcha.enabled && !captcha.token('signup')) { show(msg.signup, 'error', t('Please complete the security check.')); return 'captcha'; }
      var btn = $('signupSubmit');
      state.busy = true;
      setBusy(btn, true, t('Creating account…'), t('Create account'));
      var options = { data: { full_name: name } };
      var copt = captcha.options('signup');
      if (copt) options.captchaToken = copt.captchaToken;
      var r;
      try { r = await client.auth.signUp({ email: email, password: password, options: options }); } catch (e) { r = { error: { name: 'AuthRetryableFetchError', message: 'Failed to fetch' } }; }
      state.busy = false;
      setBusy(btn, false, '', t('Create account'));
      captcha.reset('signup');
      var kind = classifyError(r && r.error);
      if (kind === 'already_registered') return alreadyRegistered(email);
      if (kind === 'weak_password') {
        field('signupPasswordField', false);
        show(msg.signup, 'error', t('Use at least 8 characters, with letters and digits.'));
        return kind;
      }
      if (kind) { show(msg.signup, 'error', commonMessage(kind, t)); return kind; }
      var data = (r && r.data) || {};
      if (data.session) return goSignedIn();
      // "Confirm email" ON and the address already belongs to a confirmed
      // account: Supabase answers with a user without identities, no email.
      if (data.user && Array.isArray(data.user.identities) && data.user.identities.length === 0) return alreadyRegistered(email);
      openCode(email, { sentAt: now() });
      return 'code';
    }

    // -- Verify the code
    async function verify() {
      if (state.busy) return 'busy';
      var code = cleanCode(codeInput ? codeInput.value : '');
      if (code.length !== CODE_LENGTH) { show(msg.code, 'error', t('Enter the 6-digit code.')); return 'short'; }
      state.busy = true;
      state.lastTried = code;
      hide(msg.code);
      setBusy(verifyBtn, true, t('Verifying…'), t('Verify'));
      var r;
      try { r = await client.auth.verifyOtp({ email: state.email, token: code, type: 'signup' }); } catch (e) { r = { error: { name: 'AuthRetryableFetchError', message: 'Failed to fetch' } }; }
      state.busy = false;
      setBusy(verifyBtn, false, '', t('Verify'));
      var kind = classifyError(r && r.error);
      if (!kind) {
        if (r && r.data && r.data.session) return goSignedIn();
        setTab('login');
        var le = $('loginEmail');
        if (le) le.value = state.email;
        show(msg.login, 'info', t('Your email is confirmed. Log in to continue.'));
        return 'confirmed';
      }
      if (kind === 'otp') {
        var text;
        if (state.sentAt && now() - state.sentAt > CODE_TTL_MS) text = t('This code has expired. Send a new code.');
        else if (state.sentAt) text = t('This code is not correct. Check the email and try again.');
        else text = t('This code is wrong or has expired. Check it, or send a new code.');
        show(msg.code, 'error', text);
        if (codeInput && codeInput.select) codeInput.select();
        return state.sentAt && now() - state.sentAt > CODE_TTL_MS ? 'expired' : 'wrong';
      }
      show(msg.code, 'error', commonMessage(kind, t));
      return kind;
    }

    function onCodeInput() {
      if (!codeInput) return null;
      var v = cleanCode(codeInput.value);
      if (codeInput.value !== v) codeInput.value = v;
      if (v.length === CODE_LENGTH && v !== state.lastTried) return verify();
      return null;
    }

    function onCodePaste(ev) {
      var text = ev && ev.clipboardData && ev.clipboardData.getData ? ev.clipboardData.getData('text') : '';
      if (!text) return null;
      if (ev.preventDefault) ev.preventDefault();
      codeInput.value = cleanCode(text);
      return onCodeInput();
    }

    // -- Resend
    async function resend() {
      if (state.left > 0 || state.busy) return 'wait';
      if (captcha.enabled && !captcha.token('code')) { show(msg.code, 'error', t('Please complete the security check.')); return 'captcha'; }
      var args = { type: 'signup', email: state.email };
      var copt = captcha.options('code');
      if (copt) args.options = copt;
      var r;
      try { r = await client.auth.resend(args); } catch (e) { r = { error: { name: 'AuthRetryableFetchError', message: 'Failed to fetch' } }; }
      captcha.reset('code');
      var kind = classifyError(r && r.error);
      if (!kind) {
        state.sentAt = now();
        state.lastTried = '';
        show(msg.code, 'success', t('A new code is on its way. Check your inbox (and spam folder).'));
        startCountdown(RESEND_SECONDS);
        return 'sent';
      }
      show(msg.code, 'error', commonMessage(kind, t));
      if (kind === 'rate_limit') startCountdown(RESEND_SECONDS);
      return kind;
    }

    function changeEmail() {
      setTab('signup');
      var se = $('signupEmail');
      if (se) { se.value = state.email; if (se.focus) se.focus(); }
      return 'signup';
    }

    // -- wire
    if (tabLogin) tabLogin.addEventListener('click', function () { setTab('login'); });
    if (tabSignup) tabSignup.addEventListener('click', function () { setTab('signup'); });
    var googles = doc.querySelectorAll('.btn-google');
    for (var i = 0; i < googles.length; i++) googles[i].addEventListener('click', google);
    if ($('loginForm')) $('loginForm').addEventListener('submit', login);
    if ($('signupForm')) $('signupForm').addEventListener('submit', signup);
    if ($('codeForm')) $('codeForm').addEventListener('submit', function (ev) { if (ev && ev.preventDefault) ev.preventDefault(); return verify(); });
    if (codeInput) {
      codeInput.addEventListener('input', onCodeInput);
      codeInput.addEventListener('paste', onCodePaste);
    }
    if (resendBtn) resendBtn.addEventListener('click', resend);
    if ($('changeEmail')) $('changeEmail').addEventListener('click', changeEmail);
    if (win.location && /(^|[?&])tab=signup(&|$)/.test(String(win.location.search || '').slice(1))) setTab('signup');
    else setTab('login');

    return { state: state, setTab: setTab, google: google, login: login, signup: signup, verify: verify,
      onCodeInput: onCodeInput, onCodePaste: onCodePaste, resend: resend, changeEmail: changeEmail, openCode: openCode };
  }

  // ---- Forgot password -------------------------------------------------------------
  function initForgot(doc, win, client, opts) {
    opts = opts || {};
    var t = translator(doc);
    var captcha = opts.captcha || createCaptcha(doc, win);
    var form = doc.getElementById('forgotForm');
    var input = doc.getElementById('email');
    var fieldEl = doc.getElementById('emailField');
    var btn = doc.getElementById('submitBtn');
    var banner = doc.getElementById('formBanner');
    if (!form || !input) return null;
    function showBanner(kind, text) {
      if (!banner) return;
      banner.textContent = text;
      banner.className = 'auth-banner is-visible ' + (kind === 'error' ? 'auth-banner-error' : 'auth-banner-success');
    }
    async function submit(ev) {
      if (ev && ev.preventDefault) ev.preventDefault();
      if (banner) { banner.className = 'auth-banner'; banner.textContent = ''; }
      var email = String(input.value || '').trim().toLowerCase();
      var ok = EMAIL_RE.test(email);
      if (fieldEl && fieldEl.classList) fieldEl.classList.toggle('has-error', !ok);
      if (!ok) return 'invalid';
      if (captcha.enabled && !captcha.token('forgot')) { showBanner('error', t('Please complete the security check.')); return 'captcha'; }
      if (btn) { btn.disabled = true; btn.textContent = t('Sending…'); }
      var options = { redirectTo: RESET_PASSWORD_URLS[lang(doc)] };
      var copt = captcha.options('forgot');
      if (copt) options.captchaToken = copt.captchaToken;
      var r;
      try { r = await client.auth.resetPasswordForEmail(email, options); } catch (e) { r = { error: { name: 'AuthRetryableFetchError', message: 'Failed to fetch' } }; }
      if (btn) { btn.disabled = false; btn.textContent = t('Send reset link'); }
      captcha.reset('forgot');
      var kind = classifyError(r && r.error);
      if (kind) { showBanner('error', commonMessage(kind, t)); return kind; }
      // Same message whether or not the address has an account.
      showBanner('success', t('If an account exists for that email, a reset link is on its way. Check your inbox (and spam folder).'));
      if (form.reset) form.reset();
      return 'sent';
    }
    form.addEventListener('submit', submit);
    return { submit: submit };
  }

  // ---- Reset password (page opened from the email link) ------------------------------
  function initReset(doc, win, client, opts) {
    opts = opts || {};
    var t = translator(doc);
    var $ = function (id) { return doc.getElementById(id); };
    var form = $('resetForm');
    var pw = $('newPassword');
    var confirmInput = $('confirmPassword');
    var btn = $('submitBtn');
    var banner = $('formBanner');
    if (!form || !pw) return null;
    var state = { done: false };
    function showBanner(kind, text) {
      if (!banner) return;
      banner.textContent = text;
      banner.className = 'auth-banner is-visible ' + (kind === 'error' ? 'auth-banner-error' : 'auth-banner-success');
    }
    function showInvalid() {
      if ($('resetState')) $('resetState').hidden = true;
      if ($('invalidState')) $('invalidState').hidden = false;
    }
    // The link signs the user in for this one change; without a session and
    // without recovery data in the URL, the link was invalid or used.
    async function checkLink() {
      var session = null;
      try { var r = await client.auth.getSession(); session = r && r.data && r.data.session; } catch (e) { session = null; }
      var hasRecovery = /type=recovery/.test(String(win.location.hash || '')) || /[?&]code=/.test(String(win.location.search || ''));
      if (!session && !hasRecovery) { showInvalid(); return 'invalid'; }
      return 'ok';
    }
    async function submit(ev) {
      if (ev && ev.preventDefault) ev.preventDefault();
      if (state.busy || state.done) return 'busy';
      if (banner) { banner.className = 'auth-banner'; banner.textContent = ''; }
      var password = String(pw.value || '');
      var ok = passwordOk(password);
      var same = ok && confirmInput && password === String(confirmInput.value || '');
      if ($('passwordField') && $('passwordField').classList) $('passwordField').classList.toggle('has-error', !ok);
      if ($('confirmField') && $('confirmField').classList) $('confirmField').classList.toggle('has-error', ok && !same);
      if (!ok || !same) return 'invalid';
      state.busy = true;
      if (btn) { btn.disabled = true; btn.textContent = t('Updating…'); }
      var r;
      try { r = await client.auth.updateUser({ password: password }); } catch (e) { r = { error: { name: 'AuthRetryableFetchError', message: 'Failed to fetch' } }; }
      state.busy = false;
      if (btn) { btn.disabled = false; btn.textContent = t('Update password'); }
      var kind = classifyError(r && r.error);
      if (kind === 'weak_password') {
        if ($('passwordField') && $('passwordField').classList) $('passwordField').classList.toggle('has-error', true);
        showBanner('error', t('Use at least 8 characters, with letters and digits.'));
        return kind;
      }
      if (kind === 'same_password') { showBanner('error', t('Choose a password different from your current one.')); return kind; }
      if (kind) { showBanner('error', commonMessage(kind, t)); return kind; }
      state.done = true;
      showBanner('success', t('Your password has been updated. Redirecting you now…'));
      if (form.reset) form.reset();
      (win.setTimeout || setTimeout)(function () { win.location.href = HOME[lang(doc)]; }, 1500);
      return 'updated';
    }
    form.addEventListener('submit', submit);
    if (opts.checkLink !== false) (win.setTimeout || setTimeout)(checkLink, 1200);
    return { submit: submit, checkLink: checkLink };
  }

  var api = {
    TURNSTILE_SITE_KEY: TURNSTILE_SITE_KEY, OAUTH_REDIRECT_URLS: OAUTH_REDIRECT_URLS, RESET_PASSWORD_URLS: RESET_PASSWORD_URLS,
    passwordOk: passwordOk, initReset: initReset,
    I18N: I18N, cleanCode: cleanCode, classifyError: classifyError, returnTarget: returnTarget, googleOptions: googleOptions,
    createCaptcha: createCaptcha, initLogin: initLogin, initForgot: initForgot, wireChrome: wireChrome, translator: translator,
  };
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api; // local tests only
  } else {
    var start = function () {
      var client = window.__supabaseClient;
      if (!client) return;
      wireChrome(document, window, translator(document));
      if (document.getElementById('loginForm')) initLogin(document, window, client);
      if (document.getElementById('forgotForm')) initForgot(document, window, client);
      if (document.getElementById('resetForm')) initReset(document, window, client);
    };
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
    else start();
  }
})();
