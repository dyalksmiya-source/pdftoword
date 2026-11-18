/*
 * PDF2Wordly Pro checkout (Paddle Billing, Paddle.js v2).
 *
 * Hidden from normal visitors: the checkout is enabled only after a visit
 * with ?paddle=sandbox (remembered for this tab in sessionStorage;
 * ?paddle=off forgets it). Without the flag this file does nothing: Paddle.js
 * is not loaded and the Pro button keeps saying "Pro is coming soon".
 *
 * The server decides Free / Pro: after checkout.completed this page only
 * polls GET /api/me until the Paddle webhook has activated Pro.
 */
(function () {
  'use strict';

  // ---- The ONE place to switch from sandbox to live ------------------------
  // Live: env 'production', the live client-side token (live_...) and the
  // live price ID. The client-side token is public by design (not a secret).
  var PADDLE = {
    env: 'sandbox',
    clientToken: 'test_7b94c0b97260a1251ddd3166350',
    priceId: 'pri_01m7qw0f7qr17f82dtkdx560wv',
    scriptUrl: 'https://cdn.paddle.com/paddle/v2/paddle.js',
  };
  // ---------------------------------------------------------------------------

  var FLAG_KEY = 'pw-paddle';
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
    },
    fr: {
      subscribe: 'S’abonner – {price}/mois',
      activating: 'Paiement reçu, activation de Pro…',
      active: 'Pro est activé. Merci !',
      slow: 'Paiement reçu. Pro sera activé dans quelques minutes : actualisez la page.',
      failed: 'Le paiement n’a pas pu s’ouvrir. Veuillez réessayer plus tard.',
    },
    ar: {
      subscribe: 'اشترك – {price} شهريًا',
      activating: 'تم استلام الدفع، جارٍ تفعيل Pro…',
      active: 'تم تفعيل Pro. شكرًا لك!',
      slow: 'تم استلام الدفع. سيتم تفعيل Pro خلال بضع دقائق: حدّث الصفحة.',
      failed: 'تعذّر فتح صفحة الدفع. يُرجى المحاولة لاحقًا.',
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

  /** True when the hidden checkout flag is on for this tab. */
  function flagEnabled(win) {
    var value = null;
    try {
      value = new URLSearchParams(win.location.search || '').get('paddle');
    } catch (e) {
      value = null;
    }
    if (value === 'sandbox') setItem(win, FLAG_KEY, 'sandbox');
    if (value === 'off') removeItem(win, FLAG_KEY);
    return getItem(win, FLAG_KEY) === 'sandbox';
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

  function loginUrl(win) {
    var p = String(win.location.pathname || '/');
    return p.indexOf('/fr/') === 0 || p.indexOf('/ar/') === 0 ? '../login.html' : 'login.html';
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
   * Returns 'hidden' (no flag), 'redirect' (return after login),
   * 'no-button', 'error' (Paddle.js failed) or 'ready'.
   */
  async function init(doc, win, client, fetchImpl, now) {
    now = now || Date.now();
    var session = await getSession(client);
    if (takeReturn(win, !!(session && session.user), now)) return 'redirect';
    if (!flagEnabled(win)) return 'hidden';

    var placeholder = doc.getElementById('proCta');
    if (!placeholder) return 'no-button';
    var t = TEXT[lang(doc)];
    var priceEl = doc.querySelector('[data-config="pro-price"]');
    var price = (priceEl && priceEl.textContent.trim()) || '$2.99';

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
        showBadgePro(doc);
        say(t.active);
        button.disabled = true;
      } else {
        say(t.slow);
      }
    }

    try {
      paddle = await loadPaddle(doc, win);
      if (PADDLE.env === 'sandbox') paddle.Environment.set('sandbox');
      paddle.Initialize({ token: PADDLE.clientToken, eventCallback: onEvent });
    } catch (e) {
      return 'error';
    }

    var button = doc.createElement('button');
    button.type = 'button';
    button.id = 'proCta';
    button.className = 'btn btn-primary btn-full';
    button.textContent = t.subscribe.replace('{price}', price);
    placeholder.parentNode.replaceChild(button, placeholder);
    button.parentNode.insertBefore(status, button.nextSibling);

    button.addEventListener('click', async function () {
      var s = await getSession(client);
      if (!s || !s.user) {
        rememberReturn(win, Date.now());
        win.location.href = loginUrl(win);
        return;
      }
      try {
        paddle.Checkout.open({
          items: [{ priceId: PADDLE.priceId, quantity: 1 }],
          customer: { email: s.user.email },
          customData: { supabase_user_id: s.user.id },
          settings: { displayMode: 'overlay', locale: lang(doc), allowLogout: false },
        });
      } catch (e) {
        say(t.failed);
      }
    });
    return 'ready';
  }

  var api = {
    PADDLE: PADDLE, TEXT: TEXT, init: init, flagEnabled: flagEnabled, safeReturn: safeReturn,
    takeReturn: takeReturn, pollPro: pollPro, lang: lang, loginUrl: loginUrl, pricingPath: pricingPath,
  };
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api; // local tests only
  } else {
    var start = function () {
      init(document, window, window.__supabaseClient, window.fetch.bind(window));
    };
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
    else start();
  }
})();
