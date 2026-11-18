/*
 * PDF2Wordly header language switcher.
 *
 * Disclosure pattern (WAI-ARIA APG "disclosure navigation"): a button that
 * shows/hides a list of ordinary links. Keyboard: Enter/Space toggle,
 * Arrow keys / Home / End move between languages, Escape closes and returns
 * focus to the button, Tab or a click outside closes it.
 *
 * There is deliberately NO automatic redirect based on the browser language
 * (bad for SEO and for users who chose a language on purpose).
 */
(function () {
  'use strict';

  function init(doc) {
    var root = doc.querySelector('.lang-switch');
    if (!root) return;
    var button = root.querySelector('.lang-switch-btn');
    var menu = root.querySelector('.lang-switch-menu');
    if (!button || !menu) return;
    var links = Array.prototype.slice.call(menu.querySelectorAll('a'));
    if (!links.length) return;

    function isOpen() {
      return button.getAttribute('aria-expanded') === 'true';
    }

    function focusLink(index) {
      var n = links.length;
      links[((index % n) + n) % n].focus();
    }

    function open(focusIndex) {
      menu.hidden = false;
      button.setAttribute('aria-expanded', 'true');
      if (typeof focusIndex === 'number') focusLink(focusIndex);
    }

    function close(returnFocus) {
      if (!isOpen()) return;
      menu.hidden = true;
      button.setAttribute('aria-expanded', 'false');
      if (returnFocus) button.focus();
    }

    function currentIndex() {
      for (var i = 0; i < links.length; i++) {
        if (links[i].hasAttribute('aria-current')) return i;
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
        open(links.length - 1);
      } else if (e.key === 'Escape') {
        close(true);
      }
    });

    menu.addEventListener('keydown', function (e) {
      var index = links.indexOf(doc.activeElement);
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        focusLink(index + 1);
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        focusLink(index - 1);
      } else if (e.key === 'Home') {
        e.preventDefault();
        focusLink(0);
      } else if (e.key === 'End') {
        e.preventDefault();
        focusLink(links.length - 1);
      } else if (e.key === 'Escape') {
        e.preventDefault();
        close(true);
      } else if (e.key === 'Tab') {
        close(false);
      }
    });

    // Click outside closes it.
    doc.addEventListener('click', function (e) {
      if (isOpen() && !root.contains(e.target)) close(false);
    });

    // Focus leaving the switcher (e.g. Shift+Tab from the first link) closes it.
    root.addEventListener('focusout', function (e) {
      if (isOpen() && e.relatedTarget && !root.contains(e.relatedTarget)) close(false);
    });

    // Opening the mobile menu closes the language list.
    var mobileToggle = doc.getElementById('mobileMenuToggle');
    if (mobileToggle) {
      mobileToggle.addEventListener('click', function () {
        close(false);
      });
    }
  }

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { init: init }; // used by the local test only
  } else if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { init(document); });
  } else {
    init(document);
  }
})();
