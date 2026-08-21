/**
 * PDFtoWord — application script
 * Organized as small, single-purpose modules initialized from initializeApp().
 */

(function () {
  'use strict';

  /* ------------------------------------------------------------------ */
  /* Config                                                              */
  /* ------------------------------------------------------------------ */

  const MAX_FILE_SIZE_BYTES = 25 * 1024 * 1024; // 25 MB
  const ACCEPTED_TYPE = 'application/pdf';

  /* ------------------------------------------------------------------ */
  /* App state                                                           */
  /* ------------------------------------------------------------------ */

  let selectedFile = null;
  let objectUrl = null; // tracks the last created blob URL so it can be revoked

  /* ------------------------------------------------------------------ */
  /* Entry point                                                         */
  /* ------------------------------------------------------------------ */

  document.addEventListener('DOMContentLoaded', initializeApp);

  function initializeApp() {
    initializeTheme();
    initializeMobileMenu();
    initializeUpload();
    initializeFAQ();
    document.getElementById('year').textContent = new Date().getFullYear();
  }

  /* ------------------------------------------------------------------ */
  /* Theme                                                                */
  /* ------------------------------------------------------------------ */

  function initializeTheme() {
    const toggle = document.getElementById('themeToggle');
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
        question.setAttribute('aria-expanded', String(!expanded));
        answer.hidden = expanded;
      });
    });
  }

  /* ------------------------------------------------------------------ */
  /* Upload + conversion tool                                            */
  /* ------------------------------------------------------------------ */

  function initializeUpload() {
    const dropzone = document.getElementById('dropzone');
    const fileInput = document.getElementById('fileInput');
    const browseBtn = document.getElementById('browseBtn');
    const removeFileBtn = document.getElementById('removeFileBtn');
    const convertBtn = document.getElementById('convertBtn');
    const convertAnotherBtn = document.getElementById('convertAnotherBtn');
    const retryBtn = document.getElementById('retryBtn');

    // Click / keyboard triggers for the hidden file input
    browseBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      fileInput.click();
    });
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

    removeFileBtn.addEventListener('click', removeFile);
    convertBtn.addEventListener('click', startConversion);
    convertAnotherBtn.addEventListener('click', resetConverter);
    retryBtn.addEventListener('click', resetConverter);
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
    errorEl.textContent = message;
    errorEl.hidden = false;
  }

  function clearToolError() {
    const errorEl = document.getElementById('toolError');
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

    setToolState('converting');
    animateProgress();

    try {
      const result = await convertPdfToWord(selectedFile);
      showSuccessState(result);
    } catch (error) {
      showErrorState();
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

    fillEl.style.width = '0%';
    barEl.setAttribute('aria-valuenow', '0');

    stages.forEach((stage, index) => {
      window.setTimeout(() => {
        // Only update if we're still in the converting state
        if (document.getElementById('stateConverting').hidden) return;
        statusEl.textContent = stage.label;
        fillEl.style.width = stage.progress + '%';
        barEl.setAttribute('aria-valuenow', String(stage.progress));
      }, index * 500);
    });
  }

  // Base URL of the conversion API. Points at the local Flask server during
  // development; change this to your production API origin when deploying.
 const API_BASE_URL = 'https://pdftoword-production-613f.up.railway.app';

  /**
   * Sends the PDF to the conversion backend and resolves with the
   * downloadable result, or throws on failure so startConversion() can
   * show the error state.
   */
  async function convertPdfToWord(file) {
    const formData = new FormData();
    formData.append('file', file);

    const response = await fetch(API_BASE_URL + '/api/convert', {
      method: 'POST',
      body: formData,
    });

    if (!response.ok) {
      throw new Error('Conversion failed');
    }

    const blob = await response.blob();
    const fileName = file.name.replace(/\.pdf$/i, '') + '.docx';

    return {
      fileName,
      blobUrl: URL.createObjectURL(blob),
    };
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
      // No backend connected yet — keep the button present but inert.
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
