/*
 * PDF2Wordly Pro checkout (Paddle Billing, Paddle.js v2).
 *
 * Live for every visitor: on the home pages (en, fr, ar) this file loads
 * Paddle.js and turns the Pro card button into "Subscribe – $x/month".
 * Paddle.js (~17 KB) is loaded only when it is needed: when the pricing
 * section comes near the screen, when the user points at / touches / tabs
 * to the Pro button, or when the page opens on #pricing (whenNeeded).
 * Signed-out visitors go to the login page first and come back to #pricing.
 *
 * The server decides Free / Pro: after checkout.completed this page only
 * polls GET /api/me until the Paddle webhook has activated Pro, then asks
 * plan-ui.js (window.pwPlan) to update the header, menu and pricing card.
 *
 * Guard: the checkout never opens for a Pro user, nor after a refund (one
 * refund per customer: /api/me can_purchase=false). The plan shown on the
 * page is checked first, then GET /api/me is asked again right before opening.
 */
(function () {
  'use strict';

  // ---- The ONE place for the Paddle checkout settings ---------------------
  // LIVE. Product pro_01m7stw60y2bpwmq8j563f70xa, price $4.99/month (tax
  // included). The client-side token is public by design (not a secret).
  // Sandbox (tests only): env 'sandbox', token test_7b94c0b97260a1251ddd3166350,
  // price pri_01m7qw0f7qr17f82dtkdx560wv.
  var PADDLE = {
    env: 'production',
    clientToken: 'live_7207935eda621c8816f74b80377',
    priceId: 'pri_01m7sv3bfjjw2sr5se9mb34p5q',
    scriptUrl: 'https://cdn.paddle.com/paddle/v2/paddle.js',
  };
  // ---------------------------------------------------------------------------

  var RETURN_KEY = 'pw-return';
  var RETURN_MAX_AGE_MS = 30 * 60 * 1000;
  var ALLOWED_RETURNS = ['/#pricing', '/fr/#pricing', '/ar/#pricing'];
  var POLL_INTERVAL_MS = 3000;
  var POLL_TIMEOUT_MS = 60000;

  var TEXT = {
    en: {
      subscribe: 'Subscribe – {price}/month',
      activating: 'Payment received, activating Pro…',
      active: 'Pro is active. Thank you!',
      slow: 'Payment received. Pro will be active in a few minutes: refresh the page.',
      failed: 'The checkout could not open. Please try again later.',
      alreadyPro: 'You already have Pro. Manage it from your account settings.',
      checkFailed: 'We couldn’t check your plan. Please try again.',
      refunded: 'Pro can’t be purchased again on this account after a refund. Contact support@pdf2wordly.com.',
    },
    fr: {
      subscribe: 'S’abonner – {price}/mois',
      activating: 'Paiement reçu, activation de Pro…',
      active: 'Pro est activé. Merci !',
      slow: 'Paiement reçu. Pro sera activé dans quelques minutes : actualisez la page.',
      failed: 'Le paiement n’a pas pu s’ouvrir. Veuillez réessayer plus tard.',
      alreadyPro: 'Vous avez déjà Pro. Gérez-le depuis les paramètres du compte.',
      checkFailed: 'Impossible de vérifier votre formule. Veuillez réessayer.',
      refunded: 'Pro ne peut plus être acheté sur ce compte après un remboursement. Contactez support@pdf2wordly.com.',
    },
    ar: {
      subscribe: 'اشترك – {price} شهريًا',
      activating: 'تم استلام الدفع، جارٍ تفعيل Pro…',
      active: 'تم تفعيل Pro. شكرًا لك!',
      slow: 'تم استلام الدفع. سيتم تفعيل Pro خلال بضع دقائق: حدّث الصفحة.',
      failed: 'تعذّر فتح صفحة الدفع. يُرجى المحاولة لاحقًا.',
      alreadyPro: 'لديك Pro بالفعل. يمكنك إدارته من إعدادات الحساب.',
      checkFailed: 'تعذّر التحقق من خطتك. يُرجى المحاولة مرة أخرى.',
      refunded: 'لا يمكن شراء Pro مرة أخرى على هذا الحساب بعد استرداد المبلغ. تواصل مع support@pdf2wordly.com.',
    },
  };

  function lang(doc) {
    var l = String((doc.documentElement && doc.documentElement.lang) || 'en').slice(0, 2).toLowerCase();
    return TEXT[l] ? l : 'en';
  }

  function storage(win) {
    try {
      return win.sessionStorage || null;
    } catch (e) {
      return null;
    }
  }

  function getItem(win, key) {
    var s = storage(win);
    try { return s ? s.getItem(key) : null; } catch (e) { return null; }
  }

  function setItem(win, key, value) {
    var s = storage(win);
    try { if (s) s.setItem(key, value); } catch (e) { /* storage blocked */ }
  }

  function removeItem(win, key) {
    var s = storage(win);
    try { if (s) s.removeItem(key); } catch (e) { /* storage blocked */ }
  }

  /** Only the 3 pricing sections are valid return targets. */
  function safeReturn(path) {
    return ALLOWED_RETURNS.indexOf(path) !== -1 ? path : null;
  }

  function pricingPath(win) {
    var p = String(win.location.pathname || '/');
    if (p.indexOf('/fr/') === 0) return '/fr/#pricing';
    if (p.indexOf('/ar/') === 0) return '/ar/#pricing';
    return '/#pricing';
  }

  /** The login page in the same language (fr/login.html, ar/login.html, login.html). */
  function loginUrl(win) {
    return 'login.html';
  }

  function rememberReturn(win, now) {
    setItem(win, RETURN_KEY, JSON.stringify({ p: pricingPath(win), t: now }));
  }

  /**
   * After login the user lands on "/". If a recent return target is stored,
   * send them back to the pricing section they came from (once).
   */
  function takeReturn(win, signedIn, now) {
    var raw = getItem(win, RETURN_KEY);
    if (!raw || !signedIn) return null;
    removeItem(win, RETURN_KEY);
    var target = null;
    try {
      var v = JSON.parse(raw);
      if (v && typeof v.t === 'number' && now - v.t >= 0 && now - v.t <= RETURN_MAX_AGE_MS) {
        target = safeReturn(v.p);
      }
    } catch (e) {
      target = null;
    }
    if (!target) return null;
    var here = String(win.location.pathname || '/') + String(win.location.hash || '');
    if (here === target) return null;
    win.location.replace(target);
    return target;
  }

  function backendUrl(doc, win) {
    var meta = doc.querySelector('meta[name="pdf2wordly-backend"]');
    if (meta && meta.content) return meta.content.replace(/\/$/, '');
    if (win.PDF2WORDLY_BACKEND) return String(win.PDF2WORDLY_BACKEND).replace(/\/$/, '');
    var host = win.location.hostname;
    if (host === 'localhost' || host === '127.0.0.1') return 'http://localhost:5001';
    return 'https://api.pdf2wordly.com';
  }

  async function getSession(client) {
    if (!client || !client.auth) return null;
    try {
      var res = await client.auth.getSession();
      return (res && res.data && res.data.session) || null;
    } catch (e) {
      return null;
    }
  }

  /** One fresh GET /api/me. Resolves the body, or null if it failed. */
  async function fetchMe(fetchImpl, url, token) {
    try {
      var r = await fetchImpl(url, { method: 'GET', headers: { Authorization: 'Bearer ' + token } });
      if (!r.ok) return null;
      var me = await r.json();
      return me && typeof me === 'object' ? me : null;
    } catch (e) {
      return null;
    }
  }

  /** Poll /api/me until plan = pro. Resolves 'pro' or 'timeout'. */
  async function pollPro(fetchImpl, url, getToken, opts) {
    opts = opts || {};
    var interval = opts.interval || POLL_INTERVAL_MS;
    var timeout = opts.timeout || POLL_TIMEOUT_MS;
    var clock = opts.now || Date.now;
    var sleep = opts.sleep || function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };
    var start = clock();
    for (;;) {
      try {
        var token = await getToken();
        if (token) {
          var r = await fetchImpl(url, { method: 'GET', headers: { Authorization: 'Bearer ' + token } });
          if (r.ok) {
            var me = await r.json();
            if (me && me.plan === 'pro') return 'pro';
          }
        }
      } catch (e) { /* network hiccup: keep polling */ }
      if (clock() - start + interval > timeout) return 'timeout';
      await sleep(interval);
    }
  }

  function showBadgePro(doc) {
    var badges = doc.querySelectorAll('[data-plan-badge]');
    for (var i = 0; i < badges.length; i++) {
      badges[i].textContent = 'Pro';
      badges[i].setAttribute('data-plan', 'pro');
      badges[i].hidden = false;
      badges[i].removeAttribute('hidden');
    }
  }

  function loadPaddle(doc, win) {
    if (win.Paddle) return Promise.resolve(win.Paddle);
    return new Promise(function (resolve, reject) {
      var s = doc.createElement('script');
      s.src = PADDLE.scriptUrl;
      s.async = true;
      s.onload = function () { win.Paddle ? resolve(win.Paddle) : reject(new Error('Paddle missing')); };
      s.onerror = function () { reject(new Error('Paddle.js failed to load')); };
      doc.head.appendChild(s);
    });
  }

  /**
   * Resolves when Paddle.js is worth loading: the pricing card is within
   * ~600px of the screen, or the user reaches the Pro button, or the page was
   * opened on #pricing. Without IntersectionObserver: at once.
   */
  function whenNeeded(doc, win, button) {
    return new Promise(function (resolve) {
      var done = false;
      var observer = null;
      function go() {
        if (done) return;
        done = true;
        if (observer) observer.disconnect();
        resolve();
      }
      if (String(win.location && win.location.hash || '') === '#pricing') return go();
      ['pointerenter', 'touchstart', 'focus'].forEach(function (ev) {
        button.addEventListener(ev, go, { once: true, passive: true });
      });
      var target = doc.getElementById('pricing') || button;
      if (typeof win.IntersectionObserver !== 'function') return go();
      observer = new win.IntersectionObserver(function (entries) {
        if (entries.some(function (e) { return e.isIntersecting; })) go();
      }, { rootMargin: '600px 0px' });
      observer.observe(target);
    });
  }

  /**
   * Returns 'redirect' (return after login), 'no-button' (not a home page),
   * 'error' (Paddle.js failed) or 'ready'.
   * opts.waitUntilNeeded: a function returning a promise; Paddle.js is loaded
   * after it resolves (the browser start passes whenNeeded).
   */
  async function init(doc, win, client, fetchImpl, now, opts) {
    opts = opts || {};
    now = now || Date.now();
    var session = await getSession(client);
    if (takeReturn(win, !!(session && session.user), now)) return 'redirect';

    var placeholder = doc.getElementById('proCta');
    if (!placeholder) return 'no-button';
    var t = TEXT[lang(doc)];
    var priceEl = doc.querySelector('[data-config="pro-price"]');
    var price = (priceEl && priceEl.textContent.trim()) || '$4.99';

    var status = doc.createElement('p');
    status.className = 'pro-status';
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');
    status.hidden = true;

    function say(text) {
      status.textContent = text;
      status.hidden = false;
    }

    var api = backendUrl(doc, win);
    var paddle;

    async function onEvent(ev) {
      if (!ev || ev.name !== 'checkout.completed') return;
      say(t.activating);
      var result = await pollPro(fetchImpl, api + '/api/me', async function () {
        var s = await getSession(client);
        return s && s.access_token;
      });
      if (result === 'pro') {
        if (win.pwPlan) await win.pwPlan.refresh(); // header, menu, pricing: no reload
        else showBadgePro(doc);
        say(t.active);
        button.disabled = true;
      } else {
        say(t.slow);
      }
    }

    if (opts.waitUntilNeeded) await opts.waitUntilNeeded(placeholder);
    try {
      paddle = await loadPaddle(doc, win);
      if (PADDLE.env === 'sandbox') paddle.Environment.set('sandbox');
      paddle.Initialize({ token: PADDLE.clientToken, eventCallback: onEvent });
    } catch (e) {
      // Paddle.js blocked or down: the button stays disabled and says so.
      placeholder.textContent = t.failed;
      placeholder.disabled = true;
      return 'error';
    }

    var button = doc.createElement('button');
    button.type = 'button';
    button.id = 'proCta';
    button.className = 'btn btn-primary btn-full';
    button.textContent = t.subscribe.replace('{price}', price);
    placeholder.parentNode.replaceChild(button, placeholder);
    button.parentNode.insertBefore(status, button.nextSibling);

    var busy = false;
    button.addEventListener('click', async function () {
      if (busy) return 'busy';
      busy = true;
      try {
        var s = await getSession(client);
        if (!s || !s.user) {
          rememberReturn(win, Date.now());
          win.location.href = loginUrl(win);
          return 'login';
        }
        // Guard 1: the plan already shown on the page.
        if (win.pwPlan && win.pwPlan.state === 'pro') {
          say(t.alreadyPro);
          return 'already-pro';
        }
        if (win.pwPlan && win.pwPlan.me && win.pwPlan.me.can_purchase === false) {
          say(t.refunded);
          return 'refunded';
        }
        // Guard 2: ask the server again right before opening.
        var me = await fetchMe(fetchImpl, api + '/api/me', s.access_token);
        if (!me) {
          say(t.checkFailed);
          return 'check-failed';
        }
        if (me.plan === 'pro') {
          if (win.pwPlan) win.pwPlan.update(me, s.user.id);
          say(t.alreadyPro);
          return 'already-pro';
        }
        if (me.can_purchase === false) {
          say(t.refunded);
          return 'refunded';
        }
        try {
          paddle.Checkout.open({
            items: [{ priceId: PADDLE.priceId, quantity: 1 }],
            customer: { email: s.user.email },
            customData: { supabase_user_id: s.user.id },
            settings: { displayMode: 'overlay', locale: lang(doc), allowLogout: false },
          });
          return 'opened';
        } catch (e) {
          say(t.failed);
          return 'failed';
        }
      } finally {
        busy = false;
      }
    });
    return 'ready';
  }

  var api = {
    PADDLE: PADDLE, TEXT: TEXT, init: init, whenNeeded: whenNeeded, safeReturn: safeReturn,
    takeReturn: takeReturn, pollPro: pollPro, fetchMe: fetchMe, lang: lang, loginUrl: loginUrl, pricingPath: pricingPath,
  };
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api; // local tests only
  } else {
    var start = function () {
      init(document, window, window.__supabaseClient, window.fetch.bind(window), undefined, {
        waitUntilNeeded: function (button) { return whenNeeded(document, window, button); },
      });
    };
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
    else start();
  }
})();
