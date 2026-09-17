/**
 * PDF2Wordly — application script
 * - Theme, mobile menu, FAQ (as before)
 * - Authenticated conversion: every /api/convert call carries the user's
 *   Supabase access token. The backend decides Free vs Pro — the frontend
 *   NEVER does. Error codes from the backend drive user-facing messages.
 * - Plan badge: display-only (UX). Fetched from /api/me; never authoritative.
 * - Unified checkout for the buttons the per-page inline checkout blocks
 *   miss (mobile menu + [data-checkout="pro"]), plus a global client
 *   fallback that repairs those inline blocks' `supabaseClient` scope bug.
 */

(function () {
  'use strict';

  /* ------------------------------------------------------------------ */
  /* Config                                                              */
  /* ------------------------------------------------------------------ */

  const MAX_FILE_SIZE_BYTES = 25 * 1024 * 1024; // 25 MB
  const ACCEPTED_TYPE = 'application/pdf';

  // Production backend. Override per page with
  // <meta name="pdf2wordly-backend" content="https://..."> if ever needed.
  function resolveBackendUrl() {
    const meta = document.querySelector('meta[name="pdf2wordly-backend"]');
    if (meta && meta.content) return meta.content.replace(/\/$/, '');
    if (window.PDF2WORDLY_BACKEND) return String(window.PDF2WORDLY_BACKEND).replace(/\/$/, '');
    if (window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1') {
      return 'http://localhost:5001';
    }
   return 'https://pdftoword-production-613f.up.railway.app';
  }
  const API_BASE_URL = resolveBackendUrl();

  const SUPABASE_URL = 'https://yjxfxdbrjsiuevnkiiak.supabase.co';
  const SUPABASE_ANON_KEY = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InlqeGZ4ZGJyanNpdWV2bmtpaWFrIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODc1MDMyNzUsImV4cCI6MjEwMzA3OTI3NX0.K83ZZJhw8kuCeHRfgN6wO30K9SHW_pyhQxaZufWE0jo';

  // PDF2Wordly Pro Monthly — $2.99/month (must match Lemon Squeezy variant).
  const LEMON_CHECKOUT_URL =
    'https://pdf2wordly.lemonsqueezy.com/checkout/buy/539e71b2-9f13-4f62-89c3-053c690074b9';

  /* ------------------------------------------------------------------ */
  /* App state                                                           */
  /* ------------------------------------------------------------------ */

  let selectedFile = null;
  let objectUrl = null; // tracks the last created blob URL so it can be revoked
  let currentPlan = null; // display-only; backend is authoritative

  /* ------------------------------------------------------------------ */
  /* Shared Supabase singleton (fixes cross-script scope errors)         */
  /* ------------------------------------------------------------------ */

  function getSupabaseClient() {
    if (window.__supabaseClient) return window.__supabaseClient;
    if (!window.supabase || !window.supabase.createClient) return null;
    try {
      window.__supabaseClient = window.supabase.createClient(SUPABASE_URL, SUPABASE_ANON_KEY);
    } catch (e) {
      return null;
    }
    // Global fallback: several pages contain a legacy inline checkout block
    // that references a bare `supabaseClient` identifier which is scoped
    // inside another IIFE (ReferenceError on click). Assigning the singleton
    // to the global object makes that bare reference resolve correctly, so
    // every page's upgrade button works without editing each page's script.
    // (This runs at DOMContentLoaded — long before any click can happen.)
    try {
      window.supabaseClient = window.__supabaseClient;
    } catch (e) { /* ignore */ }
    return window.__supabaseClient;
  }

  async function getAccessToken() {
    const client = getSupabaseClient();
    if (!client) return null;
    try {
      const { data } = await client.auth.getSession();
      return (data && data.session && data.session.access_token) || null;
    } catch (e) {
      return null;
    }
  }

  /* ------------------------------------------------------------------ */
  /* Entry point                                                         */
  /* ------------------------------------------------------------------ */

  document.addEventListener('DOMContentLoaded', initializeApp);

  function initializeApp() {
    initializeTheme();
    initializeMobileMenu();
    initializeUpload();
    initializeFAQ();
    initializeCheckout();
    refreshPlanBadge();
    const yearEl = document.getElementById('year');
    if (yearEl) yearEl.textContent = new Date().getFullYear();
  }

  /* ------------------------------------------------------------------ */
  /* Theme                                                                */
  /* ------------------------------------------------------------------ */

  function initializeTheme() {
    const toggle = document.getElementById('themeToggle');
    if (!toggle) return;

    const stored = safeLocalStorageGet('pdftoword-theme');
    const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
    const theme = stored || (prefersDark ? 'dark' : 'light');

    applyTheme(theme);

    toggle.addEventListener('click', () => {
      const current = document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
      const next = current === 'dark' ? 'light' : 'dark';
      applyTheme(next);
      safeLocalStorageSet('pdftoword-theme', next);
    });
  }

  function applyTheme(theme) {
    if (theme === 'dark') {
      document.documentElement.setAttribute('data-theme', 'dark');
    } else {
      document.documentElement.removeAttribute('data-theme');
    }
    const toggle = document.getElementById('themeToggle');
    if (!toggle) return;
    toggle.setAttribute('aria-pressed', String(theme === 'dark'));
    toggle.setAttribute('aria-label', theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme');
  }

  function safeLocalStorageGet(key) {
    try { return window.localStorage.getItem(key); } catch (e) { return null; }
  }
  function safeLocalStorageSet(key, value) {
    try { window.localStorage.setItem(key, value); } catch (e) { /* storage unavailable, ignore */ }
  }

  /* ------------------------------------------------------------------ */
  /* Mobile menu                                                         */
  /* ------------------------------------------------------------------ */

  function initializeMobileMenu() {
    const toggle = document.getElementById('mobileMenuToggle');
    const menu = document.getElementById('mobileMenu');
    if (!toggle || !menu) return;

    toggle.addEventListener('click', () => {
      const isOpen = toggle.getAttribute('aria-expanded') === 'true';
      toggle.setAttribute('aria-expanded', String(!isOpen));
      toggle.setAttribute('aria-label', isOpen ? 'Open menu' : 'Close menu');
      menu.hidden = isOpen;
    });

    // Close the menu after a navigation link is chosen
    menu.querySelectorAll('a').forEach((link) => {
      link.addEventListener('click', () => {
        toggle.setAttribute('aria-expanded', 'false');
        toggle.setAttribute('aria-label', 'Open menu');
        menu.hidden = true;
      });
    });
  }

  /* ------------------------------------------------------------------ */
  /* FAQ accordion                                                       */
  /* ------------------------------------------------------------------ */

  function initializeFAQ() {
    const questions = document.querySelectorAll('.faq-question');
    questions.forEach((question) => {
      question.addEventListener('click', () => {
        const expanded = question.getAttribute('aria-expanded') === 'true';
        const answer = document.getElementById(question.getAttribute('aria-controls'));
        if (!answer) return;
        question.setAttribute('aria-expanded', String(!expanded));
        answer.hidden = expanded;
      });
    });
  }

  /* ------------------------------------------------------------------ */
  /* Plan badge (DISPLAY ONLY — backend verdicts are authoritative)       */
  /* ------------------------------------------------------------------ */

  function planBadgeTargets() {
    let badges = Array.prototype.slice.call(
      document.querySelectorAll('[data-plan-badge]')
    );
    if (badges.length === 0) {
      // No page ships a badge element yet: inject one into the header so
      // users see their Free/Pro state on every page without editing
      // seventeen headers. Display-only; the backend remains authoritative.
      const actions = document.querySelector('.header-actions');
      if (actions) {
        const badge = document.createElement('span');
        badge.setAttribute('data-plan-badge', '');
        badge.setAttribute('hidden', '');
        badge.className = 'plan-badge';
        actions.insertBefore(badge, actions.firstChild);
        badges = [badge];
      }
    }
    return badges;
  }

  function renderPlanBadge() {
    planBadgeTargets().forEach((el) => {
      if (!currentPlan) {
        el.textContent = '';
        el.hidden = true;
        return;
      }
      el.hidden = false;
      el.textContent = currentPlan === 'pro' ? 'Pro' : 'Free';
      el.setAttribute('data-plan', currentPlan);
    });
  }

  async function refreshPlanBadge() {
    const token = await getAccessToken();
    if (!token) {
      currentPlan = null;
      renderPlanBadge();
      return;
    }
    try {
      const response = await fetch(API_BASE_URL + '/api/me', {
        method: 'GET',
        headers: { Authorization: 'Bearer ' + token },
      });
      if (!response.ok) {
        currentPlan = null;
      } else {
        const data = await response.json();
        currentPlan = data && data.plan === 'pro' ? 'pro' : 'free';
      }
    } catch (e) {
      currentPlan = null;
    }
    renderPlanBadge();
  }

  /* ------------------------------------------------------------------ */
  /* Unified Pro checkout (works on every page, every language)          */
  /* ------------------------------------------------------------------ */

  function loginUrlForPage() {
    // ar/ and fr/ live in subfolders; their login page resolution differs.
    const path = window.location.pathname;
    if (path.indexOf('/ar/') !== -1) return '../login.html';
    if (path.indexOf('/fr/') !== -1) return '../login.html';
    return 'login.html';
  }

  function initializeCheckout() {
    // Ownership split (do NOT wire `.get-pro-btn` here): most pages already
    // attach their own inline checkout handler to `.get-pro-btn`, and adding
    // a second listener would open the checkout twice. This central wiring
    // covers exactly the buttons the inline blocks miss on every page:
    // the mobile-menu button and any future [data-checkout="pro"] hook.
    const buttons = document.querySelectorAll(
      '#getProBtnMobile, [data-checkout="pro"]'
    );
    buttons.forEach((button) => {
      if (button.dataset.checkoutWired === 'true') return;
      button.dataset.checkoutWired = 'true';
      button.addEventListener('click', handleGetPro);
    });

    // Already-Pro users should not be pushed to upgrade: relabel buttons.
    syncCheckoutButtons();
  }

  async function syncCheckoutButtons() {
    if (currentPlan === null && (await getAccessToken())) {
      await refreshPlanBadge();
    }
    if (currentPlan !== 'pro') return;
    document.querySelectorAll('#getProBtnMobile, [data-checkout="pro"]')
      .forEach((button) => {
        if (button.tagName === 'A' || button.tagName === 'BUTTON') {
          button.setAttribute('data-is-pro', 'true');
        }
      });
  }

  async function handleGetPro(e) {
    if (e) e.preventDefault();
    const client = getSupabaseClient();
    if (!client) {
      window.location.href = loginUrlForPage();
      return;
    }
    let session = null;
    try {
      const { data, error } = await client.auth.getSession();
      if (error) throw error;
      session = data && data.session;
    } catch (err) {
      showToolError('Something went wrong. Please try again later.');
      return;
    }
    if (!session || !session.user) {
      window.location.href = loginUrlForPage();
      return;
    }
    const checkoutUrl =
      LEMON_CHECKOUT_URL +
      '?checkout[custom][supabase_user_id]=' +
      encodeURIComponent(session.user.id);
    window.open(checkoutUrl, '_blank', 'noopener');
  }

  /* ------------------------------------------------------------------ */
  /* Upload + conversion tool (authenticated)                            */
  /* ------------------------------------------------------------------ */

  function initializeUpload() {
    const dropzone = document.getElementById('dropzone');
    // Not every page embeds the converter widget. Bail out quietly.
    if (!dropzone) return;

    const fileInput = document.getElementById('fileInput');
    const browseBtn = document.getElementById('browseBtn');
    const removeFileBtn = document.getElementById('removeFileBtn');
    const convertBtn = document.getElementById('convertBtn');
    const convertAnotherBtn = document.getElementById('convertAnotherBtn');
    const retryBtn = document.getElementById('retryBtn');

    // Click / keyboard triggers for the hidden file input
    if (browseBtn) {
      browseBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        fileInput.click();
      });
    }
    dropzone.addEventListener('click', () => fileInput.click());
    dropzone.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        fileInput.click();
      }
    });

    fileInput.addEventListener('change', (e) => {
      const file = e.target.files && e.target.files[0];
      if (file) handleFileSelection(file);
      fileInput.value = ''; // allow re-selecting the same file later
    });

    // Drag & drop
    ['dragenter', 'dragover'].forEach((evt) => {
      dropzone.addEventListener(evt, (e) => {
        e.preventDefault();
        e.stopPropagation();
        const isPdf = Array.from(e.dataTransfer.items || []).some(
          (item) => item.type === ACCEPTED_TYPE
        );
        dropzone.classList.add('drag-active');
        dropzone.classList.toggle('drag-invalid', e.dataTransfer.items.length > 0 && !isPdf);
      });
    });

    ['dragleave', 'dragend'].forEach((evt) => {
      dropzone.addEventListener(evt, (e) => {
        e.preventDefault();
        dropzone.classList.remove('drag-active', 'drag-invalid');
      });
    });

    dropzone.addEventListener('drop', (e) => {
      e.preventDefault();
      e.stopPropagation();
      dropzone.classList.remove('drag-active', 'drag-invalid');
      const file = e.dataTransfer.files && e.dataTransfer.files[0];
      if (file) handleFileSelection(file);
    });

    if (removeFileBtn) removeFileBtn.addEventListener('click', removeFile);
    if (convertBtn) convertBtn.addEventListener('click', startConversion);
    if (convertAnotherBtn) convertAnotherBtn.addEventListener('click', resetConverter);
    if (retryBtn) retryBtn.addEventListener('click', resetConverter);
  }

  function handleFileSelection(file) {
    const validation = validateFile(file);
    if (!validation.valid) {
      showToolError(validation.message);
      return;
    }
    clearToolError();
    selectedFile = file;
    displaySelectedFile(file);
  }

  function validateFile(file) {
    const isPdfType = file.type === ACCEPTED_TYPE || /\.pdf$/i.test(file.name);
    if (!isPdfType) {
      return { valid: false, message: 'Please select a valid PDF file.' };
    }
    if (file.size > MAX_FILE_SIZE_BYTES) {
      return { valid: false, message: 'This file is larger than the 25 MB limit.' };
    }
    return { valid: true };
  }

  function showToolError(message) {
    const errorEl = document.getElementById('toolError');
    if (!errorEl) {
      if (message) alert(message);
      return;
    }
    errorEl.textContent = message;
    errorEl.hidden = false;
  }

  function clearToolError() {
    const errorEl = document.getElementById('toolError');
    if (!errorEl) return;
    errorEl.hidden = true;
    errorEl.textContent = '';
  }

  function displaySelectedFile(file) {
    document.getElementById('fileName').textContent = file.name;
    document.getElementById('fileSize').textContent = formatFileSize(file.size);
    setToolState('selected');
  }

  function removeFile() {
    selectedFile = null;
    setToolState('empty');
    clearToolError();
  }

  function formatFileSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    const units = ['KB', 'MB', 'GB'];
    let size = bytes / 1024;
    let unitIndex = 0;
    while (size >= 1024 && unitIndex < units.length - 1) {
      size /= 1024;
      unitIndex += 1;
    }
    return size.toFixed(1) + ' ' + units[unitIndex];
  }

  /* --- State machine for the converter card ------------------------- */

  function setToolState(state) {
    const states = ['empty', 'selected', 'converting', 'success', 'failed'];
    states.forEach((s) => {
      const el = document.getElementById('state' + capitalize(s));
      if (el) el.hidden = s !== state;
    });
  }

  function capitalize(str) {
    return str.charAt(0).toUpperCase() + str.slice(1);
  }

  /* --- Conversion flow ------------------------------------------------ */

  async function startConversion() {
    if (!selectedFile) return;

    // Require login BEFORE uploading anything.
    const token = await getAccessToken();
    if (!token) {
      showToolError('Please log in to continue.');
      window.location.href = loginUrlForPage();
      return;
    }

    setToolState('converting');
    animateProgress();

    try {
      const result = await convertPdfToWord(selectedFile, token);
      showSuccessState(result);
      refreshPlanBadge(); // Free quota may just have been consumed
    } catch (error) {
      handleConversionError(error);
    }
  }

  /**
   * Runs the visible progress stages while the actual conversion request
   * is in flight. Purely cosmetic — it does not gate the real network call.
   */
  function animateProgress() {
    const statusEl = document.getElementById('convertingStatus');
    const fillEl = document.getElementById('progressFill');
    const barEl = document.getElementById('progressBar');

    const stages = [
      { label: 'Uploading...', progress: 25 },
      { label: 'Reading PDF...', progress: 55 },
      { label: 'Preparing Word document...', progress: 80 },
      { label: 'Almost done...', progress: 95 },
    ];

    if (!statusEl || !fillEl || !barEl) return;
    fillEl.style.width = '0%';
    barEl.setAttribute('aria-valuenow', '0');

    stages.forEach((stage, index) => {
      window.setTimeout(() => {
        // Only update if we're still in the converting state
        const convertingEl = document.getElementById('stateConverting');
        if (!convertingEl || convertingEl.hidden) return;
        statusEl.textContent = stage.label;
        fillEl.style.width = stage.progress + '%';
        barEl.setAttribute('aria-valuenow', String(stage.progress));
      }, index * 500);
    });
  }

  /**
   * Sends the PDF to the conversion backend with the Supabase access token.
   * Throws a ConversionError carrying the backend's machine-readable code.
   */
  async function convertPdfToWord(file, token) {
    const formData = new FormData();
    formData.append('file', file);

    let response;
    try {
      response = await fetch(API_BASE_URL + '/api/convert', {
        method: 'POST',
        headers: { Authorization: 'Bearer ' + token },
        body: formData,
      });
    } catch (e) {
      throw { code: 'NETWORK_ERROR' };
    }

    const contentType = response.headers.get('content-type') || '';
    if (response.ok) {
      const blob = await response.blob();
      const fileName = file.name.replace(/\.pdf$/i, '') + '.docx';
      return { fileName, blobUrl: URL.createObjectURL(blob) };
    }

    let code = 'SERVER_ERROR';
    if (contentType.indexOf('application/json') !== -1) {
      try {
        const data = await response.json();
        if (data && data.code) code = data.code;
      } catch (e) { /* fall through to default */ }
    }
    throw { code, status: response.status };
  }

  function friendlyMessageForCode(code) {
    switch (code) {
      case 'AUTH_REQUIRED':
      case 'INVALID_TOKEN':
        return 'Please log in to continue.';
      case 'FREE_LIMIT_REACHED':
        return 'You have used your free conversion for the last 24 hours. Try again later or upgrade to Pro for unlimited conversions.';
      case 'SUBSCRIPTION_REQUIRED':
        return 'This feature requires a Pro subscription.';
      case 'CONVERSION_FAILED':
        return "We couldn't convert this file. Please try again.";
      case 'NETWORK_ERROR':
        return 'Connection problem. Check your internet and try again.';
      default:
        return 'Something went wrong. Please try again later.';
    }
  }

  function handleConversionError(error) {
    const code = (error && error.code) || 'SERVER_ERROR';
    if (code === 'AUTH_REQUIRED' || code === 'INVALID_TOKEN') {
      showToolError(friendlyMessageForCode(code));
      setToolState('empty');
      window.location.href = loginUrlForPage();
      return;
    }
    if (code === 'FREE_LIMIT_REACHED') {
      // Quota rejections must NOT consume anything. The inline #toolError
      // lives inside the (hidden) empty-state card, so surface the message
      // on the always-visible failure card instead — same card, same retry
      // button, on all four converter pages with zero HTML changes.
      const title = document.querySelector('#stateFailed .error-title');
      const sub = document.querySelector('#stateFailed .error-sub');
      if (title) title.textContent = 'Free conversion limit reached.';
      if (sub) sub.textContent = friendlyMessageForCode(code);
      setToolState('failed');
      return;
    }
    // Genuine conversion/server failures use the failure card.
    const sub = document.querySelector('#stateFailed .error-sub');
    if (sub) sub.textContent = friendlyMessageForCode(code);
    setToolState('failed');
  }

  function showSuccessState(result) {
    document.getElementById('resultFileName').textContent = result.fileName;

    const downloadBtn = document.getElementById('downloadBtn');
    if (objectUrl) {
      URL.revokeObjectURL(objectUrl);
      objectUrl = null;
    }
    if (result.blobUrl) {
      objectUrl = result.blobUrl;
      downloadBtn.href = result.blobUrl;
      downloadBtn.setAttribute('download', result.fileName);
    } else {
      downloadBtn.href = '#';
      downloadBtn.removeAttribute('download');
    }

    setToolState('success');
  }

  function showErrorState() {
    setToolState('failed');
  }

  function resetConverter() {
    selectedFile = null;
    if (objectUrl) {
      URL.revokeObjectURL(objectUrl);
      objectUrl = null;
    }
    clearToolError();
    setToolState('empty');
  }
})();
