/*
 * PDF2Wordly plan display (every page with the site header).
 *
 * The ONE place the browser asks GET /api/me for the plan. It sets
 * <html data-plan-state="loading | signed-out | free | pro">, and style.css
 * shows the matching header button, menu badge and pricing card:
 *   - Free / signed out: "Get Pro".
 *   - Pro: a "Pro" badge linking to account.html (no purchase button), the
 *     menu says "Pro" or "Pro until <date>", the pricing card says
 *     "Your current plan" + "Manage subscription".
 *   - loading: the "Get Pro" button keeps its size but is invisible.
 *
 * First paint: a tiny inline script in each page's <head> picks the first
 * state from the Supabase session in localStorage and the plan cached here
 * in sessionStorage ("pw-plan" = {u: user id, p: plan}). That cache only
 * chooses the first render; /api/me always wins. The server decides Pro.
 *
 * API for script.js and billing.js: window.pwPlan.refresh(), .update(me),
 * .state, .me, .onChange(fn).
 *
 * Also shows "Your account has been deleted." when the page is opened with
 * ?account=deleted (account.js redirects there after a deletion).
 */
(function () {
  'use strict';

  var CACHE_KEY = 'pw-plan';
  var STATE_ATTR = 'data-plan-state';
  var LOCALES = { en: 'en-US', fr: 'fr-FR', ar: 'ar' };

  function lang(doc) {
    var l = String((doc.documentElement && doc.documentElement.getAttribute('lang')) || 'en').slice(0, 2).toLowerCase();
    return LOCALES[l] ? l : 'en';
  }

  function backendUrl(doc, win) {
    var meta = doc.querySelector('meta[name="pdf2wordly-backend"]');
    if (meta && meta.content) return meta.content.replace(/\/$/, '');
    if (win.PDF2WORDLY_BACKEND) return String(win.PDF2WORDLY_BACKEND).replace(/\/$/, '');
    var host = win.location.hostname;
    if (host === 'localhost' || host === '127.0.0.1') return 'http://localhost:5001';
    return 'https://api.pdf2wordly.com';
  }

  function readCache(win) {
    try {
      var v = JSON.parse(win.sessionStorage.getItem(CACHE_KEY) || 'null');
      return v && typeof v.u === 'string' && (v.p === 'pro' || v.p === 'free') ? v : null;
    } catch (e) {
      return null;
    }
  }

  function writeCache(win, userId, plan) {
    try {
      if (userId && plan) win.sessionStorage.setItem(CACHE_KEY, JSON.stringify({ u: userId, p: plan }));
      else win.sessionStorage.removeItem(CACHE_KEY);
    } catch (e) { /* storage blocked: no cache, /api/me still decides */ }
  }

  function formatDate(doc, iso) {
    if (!iso) return null;
    var d = new Date(iso);
    if (isNaN(d.getTime())) return null;
    try {
      return d.toLocaleDateString(LOCALES[lang(doc)], { year: 'numeric', month: 'long', day: 'numeric' });
    } catch (e) {
      return d.toISOString().slice(0, 10);
    }
  }

  /** Text of the plan badge in the avatar menu. */
  function badgeText(doc, win, state, me) {
    var t = typeof win.pwT === 'function' ? win.pwT : function (k) { return k; };
    if (state === 'pro') {
      var end = me && me.cancel_scheduled ? formatDate(doc, me.ends_at) : null;
      return end ? t('Pro until {date}').replace('{date}', end) : 'Pro';
    }
    if (state === 'free') return t('Free');
    return '';
  }

  function render(doc, win, state, me) {
    doc.documentElement.setAttribute(STATE_ATTR, state);
    var badges = doc.querySelectorAll('[data-plan-badge]');
    for (var i = 0; i < badges.length; i++) {
      var b = badges[i];
      var text = badgeText(doc, win, state, me);
      b.textContent = text;
      if (text) {
        b.hidden = false;
        b.removeAttribute('hidden');
        b.setAttribute('data-plan', state);
      } else {
        b.hidden = true;
        b.setAttribute('hidden', '');
        b.removeAttribute('data-plan');
      }
    }
  }

  async function getSession(win) {
    var client = win.__supabaseClient;
    if (!client || !client.auth) return { client: null, session: null };
    try {
      var res = await client.auth.getSession();
      return { client: client, session: (res && res.data && res.data.session) || null };
    } catch (e) {
      return { client: client, session: null };
    }
  }

  function create(doc, win, fetchImpl) {
    var listeners = [];
    var inflight = null;
    var subscribed = false;
    var plan = {
      state: doc.documentElement.getAttribute(STATE_ATTR) || 'loading',
      me: null,
      started: false,
      onChange: function (fn) { listeners.push(fn); },
    };

    function set(state, me) {
      plan.state = state;
      plan.me = me || null;
      render(doc, win, state, plan.me);
      listeners.forEach(function (fn) { try { fn(state, plan.me); } catch (e) { /* listener error */ } });
    }

    /** Apply a /api/me body for the signed-in user (no request). */
    plan.update = function (me, userId) {
      var p = me && me.plan === 'pro' ? 'pro' : 'free';
      if (userId) writeCache(win, userId, p);
      set(p, me);
      return { plan: p, me: me };
    };

    function subscribe(client) {
      if (subscribed || !client || !client.auth || !client.auth.onAuthStateChange) return;
      subscribed = true;
      client.auth.onAuthStateChange(function (event) {
        if (event === 'SIGNED_IN' || event === 'SIGNED_OUT' || event === 'USER_UPDATED') plan.refresh();
      });
    }

    /** Ask /api/me. Resolves {plan: 'pro'|'free'|null, me}. Calls in flight are shared. */
    plan.refresh = function () {
      plan.started = true;
      if (inflight) return inflight;
      inflight = (async function () {
        var s = await getSession(win);
        subscribe(s.client);
        var session = s.session;
        if (!session || !session.user) {
          writeCache(win, null, null);
          set('signed-out', null);
          return { plan: null, me: null };
        }
        var userId = session.user.id;
        try {
          var r = await fetchImpl(backendUrl(doc, win) + '/api/me', {
            method: 'GET',
            headers: { Authorization: 'Bearer ' + session.access_token },
          });
          if (!r.ok) throw new Error('HTTP ' + r.status);
          return plan.update(await r.json(), userId);
        } catch (e) {
          // /api/me unreachable: keep this user's last known plan, else Free.
          // (The checkout re-checks /api/me itself before opening.)
          var cached = readCache(win);
          var p = cached && cached.u === userId ? cached.p : 'free';
          set(p, plan.me);
          return { plan: p, me: plan.me };
        }
      })();
      inflight.then(function () { inflight = null; }, function () { inflight = null; });
      return inflight;
    };

    return plan;
  }

  /** One-time notice after an account deletion (?account=deleted). */
  function showAccountNotice(doc, win) {
    var params;
    try {
      params = new URLSearchParams(win.location.search || '');
    } catch (e) {
      return false;
    }
    if (params.get('account') !== 'deleted') return false;
    var t = typeof win.pwT === 'function' ? win.pwT : function (k) { return k; };
    var box = doc.createElement('div');
    box.className = 'site-notice';
    box.setAttribute('role', 'status');
    var text = doc.createElement('p');
    text.textContent = t('Your account has been deleted.');
    var close = doc.createElement('button');
    close.type = 'button';
    close.className = 'site-notice-close';
    close.setAttribute('aria-label', t('Close'));
    close.textContent = '×';
    close.addEventListener('click', function () {
      if (box.parentNode) box.parentNode.removeChild(box);
    });
    box.appendChild(text);
    box.appendChild(close);
    var main = doc.getElementById('main');
    if (main && main.parentNode) main.parentNode.insertBefore(box, main);
    else if (doc.body) doc.body.insertBefore(box, doc.body.firstChild);
    try {
      params.delete('account');
      var q = params.toString();
      win.history.replaceState(null, '', win.location.pathname + (q ? '?' + q : '') + (win.location.hash || ''));
    } catch (e) { /* old browser: the notice simply shows again on reload */ }
    return true;
  }

  var api = { create: create, render: render, badgeText: badgeText, readCache: readCache, CACHE_KEY: CACHE_KEY,
    showAccountNotice: showAccountNotice };
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api; // local tests only
  } else {
    var plan = create(document, window, window.fetch.bind(window));
    window.pwPlan = plan;
    // script.js (loaded right after this file) starts the first refresh;
    // this is only a safety net for a page without it.
    var start = function () {
      if (!plan.started) plan.refresh();
      showAccountNotice(document, window);
    };
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
    else start();
  }
})();
