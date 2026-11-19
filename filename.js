/*
 * PDF2Wordly file names (converter pages): display + download name.
 *
 * - decodeName: names that carry percent-style encoded UTF-8, as "%D9%82..."
 *   or as Scribd-like "D9-82-D8-B5" hex runs, are shown decoded when the bytes
 *   are valid UTF-8 (Arabic names become readable); otherwise unchanged.
 * - render: one line, ellipsis in the MIDDLE (start ... end + ".pdf"), full
 *   name in a title tooltip. Never wider than its container (CSS min-width 0).
 * - docxName: same base name, decoded, unsafe characters removed, at most
 *   80 characters before ".docx", never empty ("converted.docx").
 * The server applies the same rules to its Content-Disposition name.
 */
(function () {
  'use strict';

  var MAX_BASE = 80;
  var END_KEEP = 10; // characters kept before the extension, after the ellipsis

  function utf8(bytes) {
    try {
      var text = new TextDecoder('utf-8', { fatal: true }).decode(new Uint8Array(bytes));
      return /[\u0000-\u001F\u007F]/.test(text) ? null : text;
    } catch (e) {
      return null;
    }
  }

  /** "D9-82-D8-B5" runs (2+ hex bytes joined by "-", at least one >= 0x80). */
  function decodeHexRuns(name) {
    // no lookbehind (older iOS Safari): the character before the run is captured and kept
    return name.replace(/(^|[^0-9A-Fa-f])((?:[0-9A-Fa-f]{2}-)+[0-9A-Fa-f]{2})(?=$|[^0-9A-Fa-f])/g, function (all, before, run) {
      var bytes = run.split('-').map(function (h) { return parseInt(h, 16); });
      if (!bytes.some(function (b) { return b >= 0x80; })) return all;
      var text = utf8(bytes);
      return text === null ? all : before + text;
    });
  }

  /** "%D9%82" sequences. */
  function decodePercent(name) {
    if (!/%[0-9A-Fa-f]{2}/.test(name)) return name;
    return name.replace(/(?:%[0-9A-Fa-f]{2})+/g, function (seq) {
      var bytes = seq.slice(1).split('%').map(function (h) { return parseInt(h, 16); });
      var text = utf8(bytes);
      return text === null ? seq : text;
    });
  }

  function decodeName(name) {
    name = String(name == null ? '' : name);
    return decodeHexRuns(decodePercent(name));
  }

  function splitExt(name) {
    var m = /^(.*?)(\.[A-Za-z0-9]{1,5})$/.exec(name);
    return m && m[1] ? { base: m[1], ext: m[2] } : { base: name, ext: '' };
  }

  /** Safe .docx name: decoded, no path / control / reserved characters, <= 80 chars. */
  function docxName(original) {
    var base = decodeName(original).replace(/\.[A-Za-z0-9]{1,5}$/, '');
    base = base
      .replace(/[\u0000-\u001F\u007F]/g, '')
      .replace(/[\\/:*?"<>|]/g, '-')
      .replace(/\s+/g, ' ')
      .replace(/-{2,}/g, '-')
      .replace(/^[\s.\-_]+|[\s.\-_]+$/g, '');
    if (!/[\p{L}\p{N}]/u.test(base)) base = '';
    if (Array.from(base).length > MAX_BASE) base = Array.from(base).slice(0, MAX_BASE).join('').replace(/[\s.\-_]+$/, '');
    return (base || 'converted') + '.docx';
  }

  /** Fill `el` with a one-line, middle-ellipsis name; full name in the tooltip. */
  function render(el, name) {
    var doc = el.ownerDocument;
    var shown = decodeName(name);
    var parts = splitExt(shown);
    var chars = Array.from(parts.base);
    var head = chars.length > END_KEEP ? chars.slice(0, chars.length - END_KEEP).join('') : '';
    var tail = (chars.length > END_KEEP ? chars.slice(chars.length - END_KEEP).join('') : parts.base) + parts.ext;
    el.textContent = '';
    el.setAttribute('title', shown);
    el.setAttribute('dir', 'auto');
    el.classList.add('file-name-split');
    if (head) {
      var a = doc.createElement('span');
      a.className = 'file-name-start';
      a.textContent = head;
      el.appendChild(a);
    }
    var b = doc.createElement('span');
    b.className = 'file-name-end';
    b.textContent = tail;
    el.appendChild(b);
    return shown;
  }

  var api = { decodeName: decodeName, docxName: docxName, render: render, splitExt: splitExt, MAX_BASE: MAX_BASE };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (typeof window !== 'undefined') window.pwFileName = api;
})();
