/*
 * PDF2Wordly account page (account.html).
 *
 * - Signed-out visitors are sent to login.html (login itself is unchanged).
 * - Signed-in users see their email, plan and subscription status, read from
 *   GET /api/me (the server decides Free / Pro; this page only displays it).
 * - Pro users with a Paddle subscription get "Manage subscription" and
 *   "Cancel subscription": both ask POST /api/billing/portal for a Paddle
 *   customer portal link and open it.
 * - "Delete account" stays disabled until the server endpoint exists.
 */
(function () {
  'use strict';

  var ERROR_TEXT = "We couldn't load your plan. Please try again later.";
  var PORTAL_ERROR_TEXT = "We couldn't open the subscription page. Please try again later.";

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
    // The server decides Pro (billing.is_pro), so the plan shown here always
    // matches what conversions actually allow.
    var status = String((me && me.subscription_status) || '').toLowerCase();
    if (me && me.plan === 'pro') {
      var ends = formatDate(me.ends_at);
      if (me.cancel_scheduled) {
        return { plan: 'Pro', status: ends ? 'Cancellation scheduled: Pro until ' + ends : 'Cancellation scheduled' };
      }
      if (status === 'canceled' || status === 'cancelled') {
        return { plan: 'Pro', status: ends ? 'Cancelled: Pro until ' + ends : 'Cancelled' };
      }
      if (status === 'past_due') {
        return { plan: 'Pro', status: 'Payment problem: please update your payment method' };
      }
      var date = formatDate(me.renews_at || me.current_period_end);
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

  /** Show the subscription buttons and wire them to the customer portal. */
  function setupBilling(doc, win, me, api, token, fetchImpl) {
    var box = doc.getElementById('billingActions');
    if (!box) return false;
    if (!(me && me.plan === 'pro' && me.billing_source === 'paddle')) {
      box.hidden = true;
      return false;
    }
    box.hidden = false;
    var err = doc.getElementById('billingError');
    async function open(which, button) {
      button.disabled = true;
      if (err) err.hidden = true;
      try {
        var r = await fetchImpl(api + '/api/billing/portal', {
          method: 'POST',
          headers: { Authorization: 'Bearer ' + token },
        });
        if (!r.ok) throw new Error('HTTP ' + r.status);
        var links = await r.json();
        var url = (which === 'cancel' && links.cancel_url) || links.overview_url;
        if (!url || url.indexOf('https://') !== 0) throw new Error('no link');
        win.location.href = url;
        return url;
      } catch (e) {
        if (err) {
          err.textContent = PORTAL_ERROR_TEXT;
          err.hidden = false;
        }
        button.disabled = false;
        return null;
      }
    }
    var manage = doc.getElementById('manageSubscription');
    var cancel = doc.getElementById('cancelSubscription');
    if (manage) manage.addEventListener('click', function () { return open('manage', manage); });
    if (cancel) cancel.addEventListener('click', function () { return open('cancel', cancel); });
    return true;
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
      var api = backendUrl(doc, win);
      var r = await fetchImpl(api + '/api/me', {
        method: 'GET',
        headers: { Authorization: 'Bearer ' + session.access_token },
      });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      var me = await r.json();
      var info = describe(me);
      setText(doc, 'accountPlan', info.plan);
      setText(doc, 'accountStatus', info.status);
      setupBilling(doc, win, me, api, session.access_token, fetchImpl);
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
    module.exports = { init: init, describe: describe, backendUrl: backendUrl, setupBilling: setupBilling }; // local tests only
  } else {
    var start = function () {
      init(document, window, window.__supabaseClient, window.fetch.bind(window));
    };
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
    else start();
  }
})();
