/*
 * PDF2Wordly header dropdowns: language switcher and account menu.
 *
 * Disclosure pattern (WAI-ARIA APG): a button that shows/hides a panel.
 * Keyboard: Enter/Space toggle, Arrow keys / Home / End move between the
 * focusable items, Escape closes and returns focus to the button, Tab or a
 * click outside closes it.
 *
 * The account menu only changes how the menu opens and closes. Sign-in state,
 * the email and Log out are still handled by each page's existing auth script
 * (same element ids). The menu closes when the session ends.
 *
 * There is deliberately NO automatic redirect based on the browser language.
 */
(function () {
  'use strict';

  function setupDropdown(doc, root, button, panel) {
    if (!root || !button || !panel) return null;

    function items() {
      return Array.prototype.slice.call(panel.querySelectorAll('a, button'));
    }

    function isOpen() {
      return button.getAttribute('aria-expanded') === 'true';
    }

    function focusItem(index) {
      var list = items();
      if (!list.length) return;
      var n = list.length;
      list[((index % n) + n) % n].focus();
    }

    function open(focusIndex) {
      panel.hidden = false;
      button.setAttribute('aria-expanded', 'true');
      if (typeof focusIndex === 'number') focusItem(focusIndex);
    }

    function close(returnFocus) {
      if (!isOpen()) return;
      panel.hidden = true;
      button.setAttribute('aria-expanded', 'false');
      if (returnFocus) button.focus();
    }

    function currentIndex() {
      var list = items();
      for (var i = 0; i < list.length; i++) {
        if (list[i].hasAttribute('aria-current')) return i;
      }
      return 0;
    }

    button.addEventListener('click', function () {
      if (isOpen()) close(false);
      else open(currentIndex());
    });

    button.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        open(0);
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        open(items().length - 1);
      } else if (e.key === 'Escape') {
        close(true);
      }
    });

    panel.addEventListener('keydown', function (e) {
      var index = items().indexOf(doc.activeElement);
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        focusItem(index + 1);
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        focusItem(index - 1);
      } else if (e.key === 'Home') {
        e.preventDefault();
        focusItem(0);
      } else if (e.key === 'End') {
        e.preventDefault();
        focusItem(items().length - 1);
      } else if (e.key === 'Escape') {
        e.preventDefault();
        close(true);
      } else if (e.key === 'Tab') {
        close(false);
      }
    });

    doc.addEventListener('click', function (e) {
      if (isOpen() && !root.contains(e.target)) close(false);
    });

    root.addEventListener('focusout', function (e) {
      if (isOpen() && e.relatedTarget && !root.contains(e.relatedTarget)) close(false);
    });

    var mobileToggle = doc.getElementById('mobileMenuToggle');
    if (mobileToggle) {
      mobileToggle.addEventListener('click', function () {
        close(false);
      });
    }

    return { open: open, close: close, isOpen: isOpen };
  }

  function init(doc, Observer) {
    // Language switcher.
    var lang = doc.querySelector('.lang-switch');
    if (lang) {
      setupDropdown(doc, lang, lang.querySelector('.lang-switch-btn'), lang.querySelector('.lang-switch-menu'));
    }

    // Account menu.
    var account = doc.getElementById('profileChip');
    if (account) {
      var menu = setupDropdown(doc, account, account.querySelector('.account-btn'), doc.getElementById('profileMenu'));
      var email = doc.getElementById('profileMenuEmail');
      if (menu && Observer) {
        // Session ended (Log out here or in another tab): close the menu.
        new Observer(function () {
          if (account.getAttribute('data-active') !== 'true') menu.close(false);
        }).observe(account, { attributes: true, attributeFilter: ['data-active'] });
      }
      if (email && Observer) {
        // Full email as a tooltip, since the visible line is shortened with "…".
        var syncTitle = function () { email.title = email.textContent || ''; };
        new Observer(syncTitle).observe(email, { childList: true, characterData: true, subtree: true });
        syncTitle();
      }
    }
  }

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { init: init }; // used by the local test only
  } else {
    var start = function () { init(document, window.MutationObserver); };
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
    else start();
  }
})();
