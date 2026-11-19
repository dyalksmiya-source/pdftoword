/*
 * PDF2Wordly account page (account.html).
 *
 * - Signed-out visitors are sent to login.html (login itself is unchanged).
 * - Signed-in users see their email, plan and subscription status, read from
 *   GET /api/me (the server decides Free / Pro; this page only displays it).
 * - Pro users with a Paddle subscription get "Manage subscription" and
 *   "Cancel subscription": both ask POST /api/billing/portal for a Paddle
 *   customer portal link and open it. When a cancellation is already
 *   scheduled, only "Manage subscription" stays (Paddle's portal lets the
 *   user resume) and a short note says the subscription will not renew.
 * - "Delete account" opens a dialog; the user types DELETE, then
 *   DELETE /api/account. On success: local sign-out and redirect to the home
 *   page with ?account=deleted (plan-ui.js shows the notice). On 409
 *   (renewing subscription): the cancel-first message with a link that opens
 *   "Manage subscription".
 */
(function () {
  'use strict';

  var ERROR_TEXT = "We couldn't load your plan. Please try again later.";
  var PORTAL_ERROR_TEXT = "We couldn't open the subscription page. Please try again later.";
  var DELETE_TEXT = {
    blocked: 'You have a Pro subscription that will renew. Cancel it first in ',
    manage: 'Manage subscription',
    blockedEnd: ', then delete your account.',
    expired: 'Your session has expired. Please log in again, then try again.',
    limited: 'Too many attempts. Please try again later.',
    failed: "We couldn't delete your account. Please try again later.",
    working: 'Deleting…',
    confirm: 'Delete my account',
  };
  var SUPABASE_STORAGE_KEY = 'sb-yjxfxdbrjsiuevnkiiak-auth-token';
  var WILL_NOT_RENEW_TEXT = 'Your subscription will not renew. You can resume it from Manage subscription.';

  /** Site i18n (pwT from script.js); English when it is not there. */
  function tr(win, text) {
    return win && typeof win.pwT === 'function' ? win.pwT(text) : text;
  }

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
    var scheduled = !!me.cancel_scheduled;
    var cancelButton = doc.getElementById('cancelSubscription');
    if (cancelButton) cancelButton.hidden = scheduled;
    var note = doc.getElementById('billingNote');
    if (note) {
      note.textContent = scheduled ? tr(win, WILL_NOT_RENEW_TEXT) : '';
      note.hidden = !scheduled;
    }
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

  /** Remove the local session after the account is gone (no server call). */
  async function signOutLocal(client, win) {
    try {
      if (client && client.auth && client.auth.signOut) await client.auth.signOut({ scope: 'local' });
    } catch (e) { /* the user no longer exists: ignore */ }
    try { win.localStorage.removeItem(SUPABASE_STORAGE_KEY); } catch (e) { /* storage blocked */ }
    try { win.sessionStorage.removeItem('pw-plan'); } catch (e) { /* storage blocked */ }
  }

  /**
   * Delete-account button + confirmation dialog.
   * getToken() -> current access token. Returns the handlers for tests.
   */
  function setupDelete(doc, win, client, api, getToken, fetchImpl, state) {
    state = state || {};
    var openBtn = doc.getElementById('deleteAccountBtn');
    var dialog = doc.getElementById('deleteDialog');
    var form = doc.getElementById('deleteForm');
    var input = doc.getElementById('deleteConfirmInput');
    var confirmBtn = doc.getElementById('deleteConfirm');
    var cancelBtn = doc.getElementById('deleteCancel');
    var dialogErr = doc.getElementById('deleteDialogError');
    var pageErr = doc.getElementById('deleteError');
    if (!openBtn || !dialog || !form || !input || !confirmBtn) return null;

    function showError(el, text) {
      if (!el) return;
      el.textContent = text;
      el.hidden = false;
    }
    function closeDialog() {
      if (typeof dialog.close === 'function') dialog.close();
      else dialog.removeAttribute('open');
    }
    function open() {
      input.value = '';
      confirmBtn.disabled = true;
      confirmBtn.textContent = DELETE_TEXT.confirm;
      if (dialogErr) dialogErr.hidden = true;
      if (pageErr) pageErr.hidden = true;
      if (typeof dialog.showModal === 'function') dialog.showModal();
      else dialog.setAttribute('open', '');
      if (input.focus) input.focus();
    }
    function onInput() {
      confirmBtn.disabled = input.value !== 'DELETE';
    }
    function showBlocked() {
      closeDialog();
      if (!pageErr) return;
      pageErr.textContent = '';
      pageErr.appendChild(doc.createTextNode(DELETE_TEXT.blocked));
      var link = doc.createElement('a');
      link.href = '#billingActions';
      link.textContent = DELETE_TEXT.manage;
      link.addEventListener('click', function (ev) {
        var manage = doc.getElementById('manageSubscription');
        if (manage && manage.click) {
          if (ev && ev.preventDefault) ev.preventDefault();
          manage.click();
        }
      });
      pageErr.appendChild(link);
      pageErr.appendChild(doc.createTextNode(DELETE_TEXT.blockedEnd));
      pageErr.hidden = false;
    }
    async function submit(ev) {
      if (ev && ev.preventDefault) ev.preventDefault();
      if (input.value !== 'DELETE' || state.deleting) return 'not-confirmed';
      state.deleting = true;
      confirmBtn.disabled = true;
      confirmBtn.textContent = DELETE_TEXT.working;
      if (dialogErr) dialogErr.hidden = true;
      var result;
      try {
        var token = await getToken();
        if (!token) {
          showError(dialogErr, DELETE_TEXT.expired);
          result = 'expired';
        } else {
          var r = await fetchImpl(api + '/api/account', {
            method: 'DELETE',
            headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' },
            body: JSON.stringify({ confirm: 'DELETE' }),
          });
          if (r.ok) {
            state.deleted = true;
            await signOutLocal(client, win);
            win.location.replace('index.html?account=deleted');
            return 'deleted';
          }
          if (r.status === 409) { showBlocked(); result = 'blocked'; }
          else if (r.status === 401) { showError(dialogErr, DELETE_TEXT.expired); result = 'expired'; }
          else if (r.status === 429) { showError(dialogErr, DELETE_TEXT.limited); result = 'limited'; }
          else { showError(dialogErr, DELETE_TEXT.failed); result = 'failed'; }
        }
      } catch (e) {
        showError(dialogErr, DELETE_TEXT.failed);
        result = 'failed';
      }
      state.deleting = false;
      confirmBtn.textContent = DELETE_TEXT.confirm;
      onInput();
      return result;
    }

    openBtn.addEventListener('click', open);
    input.addEventListener('input', onInput);
    form.addEventListener('submit', submit);
    if (cancelBtn) cancelBtn.addEventListener('click', closeDialog);
    return { open: open, submit: submit, onInput: onInput };
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

    // Signing out on this page sends the user to the login page
    // (not after a deletion: that one goes to the home page).
    var deleteState = {};
    if (client.auth.onAuthStateChange) {
      client.auth.onAuthStateChange(function (_event, s) {
        if (!s && !deleteState.deleted) win.location.replace(loginUrl);
      });
    }

    setText(doc, 'accountEmail', session.user.email || '');
    var api = backendUrl(doc, win);
    setupDelete(doc, win, client, api, async function () {
      try {
        var res2 = await client.auth.getSession();
        var s2 = res2 && res2.data && res2.data.session;
        return s2 && s2.access_token;
      } catch (e) {
        return null;
      }
    }, fetchImpl, deleteState);
    try {
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
    module.exports = { init: init, describe: describe, backendUrl: backendUrl, setupBilling: setupBilling, setupDelete: setupDelete }; // local tests only
  } else {
    var start = function () {
      init(document, window, window.__supabaseClient, window.fetch.bind(window));
    };
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
    else start();
  }
})();
