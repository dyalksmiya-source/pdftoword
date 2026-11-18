/*
 * PDF2Wordly account page (account.html).
 *
 * - Signed-out visitors are sent to login.html (login itself is unchanged).
 * - Signed-in users see their email, plan and subscription status, read from
 *   GET /api/me (the server decides Free / Pro; this page only displays it).
 * - "Delete account" stays disabled until the server endpoint exists.
 */
(function () {
  'use strict';

  var ERROR_TEXT = "We couldn't load your plan. Please try again later.";

  /** Same backend resolution as script.js. */
  function backendUrl(doc, win) {
    var meta = doc.querySelector('meta[name="pdf2wordly-backend"]');
    if (meta && meta.content) return meta.content.replace(/\/$/, '');
    if (win.PDF2WORDLY_BACKEND) return String(win.PDF2WORDLY_BACKEND).replace(/\/$/, '');
    var host = win.location.hostname;
    if (host === 'localhost' || host === '127.0.0.1') return 'http://localhost:5001';
    return 'https://api.pdf2wordly.com';
  }

  function formatDate(iso) {
    if (!iso) return null;
    var d = new Date(iso);
    if (isNaN(d.getTime())) return null;
    return d.toLocaleDateString('en-US', { year: 'numeric', month: 'long', day: 'numeric' });
  }

  /** Plan and subscription text from the /api/me response. */
  function describe(me) {
    // The server grants Pro only for active/trialing subscriptions, so the plan
    // shown here always matches what conversions actually allow.
    // TODO(Paddle step): show "Pro until <date>" for a cancellation scheduled at
    // the end of the paid period (status stays active until then).
    var status = String((me && me.subscription_status) || '').toLowerCase();
    var date = formatDate(me && me.current_period_end);
    if (me && me.plan === 'pro') {
      return {
        plan: 'Pro',
        status: date ? 'Active, renews on ' + date : 'Active',
      };
    }
    if (status === 'canceled' || status === 'cancelled') {
      return { plan: 'Free', status: 'Cancelled' };
    }
    if (status === 'past_due') {
      return { plan: 'Free', status: 'Payment problem: please update your payment method' };
    }
    return { plan: 'Free', status: 'No subscription' };
  }

  function setText(doc, id, text) {
    var el = doc.getElementById(id);
    if (el) el.textContent = text;
  }

  async function init(doc, win, client, fetchImpl) {
    var loginUrl = 'login.html';
    if (!client) {
      win.location.replace(loginUrl);
      return 'redirect';
    }
    var session = null;
    try {
      var res = await client.auth.getSession();
      session = res && res.data && res.data.session;
    } catch (e) {
      session = null;
    }
    if (!session || !session.user) {
      win.location.replace(loginUrl);
      return 'redirect';
    }

    // Signing out on this page sends the user to the login page.
    if (client.auth.onAuthStateChange) {
      client.auth.onAuthStateChange(function (_event, s) {
        if (!s) win.location.replace(loginUrl);
      });
    }

    setText(doc, 'accountEmail', session.user.email || '');
    try {
      var r = await fetchImpl(backendUrl(doc, win) + '/api/me', {
        method: 'GET',
        headers: { Authorization: 'Bearer ' + session.access_token },
      });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      var info = describe(await r.json());
      setText(doc, 'accountPlan', info.plan);
      setText(doc, 'accountStatus', info.status);
      return 'ok';
    } catch (e) {
      setText(doc, 'accountPlan', '—');
      setText(doc, 'accountStatus', '—');
      var err = doc.getElementById('accountError');
      if (err) {
        err.textContent = ERROR_TEXT;
        err.hidden = false;
      }
      return 'error';
    }
  }

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { init: init, describe: describe, backendUrl: backendUrl }; // local tests only
  } else {
    var start = function () {
      init(document, window, window.__supabaseClient, window.fetch.bind(window));
    };
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
    else start();
  }
})();
