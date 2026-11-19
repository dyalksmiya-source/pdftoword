// filename.js: display (middle ellipsis, decoded names, tooltip) and the
// .docx name used for the download. Run from website/: node _tests/filename_test.js
'use strict';
const path = require('path');
const fs = require('fs');
const F = require(path.join(__dirname, '..', 'filename.js'));

let passed = 0;
const ok = (c, m) => { if (!c) throw new Error('FAIL: ' + m); passed++; console.log('ok -', m); };

const SCRIBD = 'www.scribd.com-document-738101487-D9-82-D8-B5-D8-B5-2-D9-84-D9-84-D8-B3-D9-86-D8-A9-D8-A7-D9-84-D8-A3-D9-88-D9-84-D9-89-D8-A7-D8-A8-D8-AA-D8-AF-D8-A7-D8-A6-D9-8A.pdf';

// ---- decode
ok(F.decodeName(SCRIBD) === 'www.scribd.com-document-738101487-قصص-2-للسنةالأولىابتدائي.pdf', 'Scribd-style hex runs decoded (Arabic readable)');
ok(F.decodeName('%D8%AA%D9%82%D8%B1%D9%8A%D8%B1.pdf') === 'تقرير.pdf', '%-encoded UTF-8 decoded');
ok(F.decodeName('file-AB-CD.pdf') === 'file-AB-CD.pdf', 'hex-looking but not UTF-8: unchanged');
ok(F.decodeName('10-20-30.pdf') === '10-20-30.pdf', 'ASCII-only bytes: unchanged');
ok(F.decodeName('%E2%28%A1.pdf') === '%E2%28%A1.pdf', 'invalid UTF-8 stays encoded, as is');
ok(F.decodeName('%0A.pdf') === '%0A.pdf', 'control characters never decoded');
ok(F.decodeName('تقرير.pdf') === 'تقرير.pdf', 'plain Arabic unchanged');
ok(!/\(\?</.test(fs.readFileSync(path.join(__dirname, '..', 'filename.js'), 'utf8')), 'no regex lookbehind (older iOS Safari)');

// ---- download name
ok(F.docxName(SCRIBD) === 'www.scribd.com-document-738101487-قصص-2-للسنةالأولىابتدائي.docx', 'download: decoded Scribd name');
ok(F.docxName('x'.repeat(300) + '.pdf') === 'x'.repeat(80) + '.docx', 'download: at most 80 characters before .docx');
ok(F.docxName('ع'.repeat(120) + '.pdf') === 'ع'.repeat(80) + '.docx', 'download: 80 Arabic characters (not bytes)');
for (const n of ['???.pdf', '---.pdf', '%%%.pdf', '  .pdf', '.pdf', '***.PDF']) ok(F.docxName(n) === 'converted.docx', `download: "${n}" -> converted.docx`);
ok(F.docxName('a<b>c:d"e|f?g*h.pdf') === 'a-b-c-d-e-f-g-h.docx', 'download: reserved characters replaced');
ok(F.docxName('Résumé final (v2).PDF') === 'Résumé final (v2).docx', 'download: accents and brackets kept');

// ---- render (tiny DOM)
function el() {
  const e = { children: [], attrs: {}, classes: new Set(), ownerDocument: null, _t: '',
    classList: { add: c => e.classes.add(c) },
    setAttribute(k, v) { e.attrs[k] = String(v); },
    appendChild(c) { e.children.push(c); return c; },
    get textContent() { return e.children.length ? e.children.map(c => c.textContent).join('') : e._t; },
    set textContent(v) { e._t = v; e.children = []; } };
  e.ownerDocument = { createElement: () => { const s = el(); return s; } };
  return e;
}
let box = el();
let shown = F.render(box, SCRIBD);
ok(shown === F.decodeName(SCRIBD) && box.attrs.title === shown, 'render: full decoded name in the title tooltip');
ok(box.attrs.dir === 'auto' && box.classes.has('file-name-split'), 'render: dir=auto (Arabic names right-to-left), split class');
ok(box.children.length === 2 && box.children[0].className === 'file-name-start' && box.children[1].className === 'file-name-end', 'render: start part (shrinks with …) + end part');
ok(box.children[1].textContent.endsWith('.pdf') && Array.from(box.children[1].textContent).length === 14, 'render: the end keeps the last 10 characters + ".pdf"');
ok(box.textContent === shown, 'render: nothing lost, only split');
box = el(); F.render(box, 'cv.pdf');
ok(box.children.length === 1 && box.children[0].textContent === 'cv.pdf', 'render: short name in one piece');
box = el(); F.render(box, 'تقرير المشروع النهائي للفصل الأول.pdf');
ok(box.textContent === 'تقرير المشروع النهائي للفصل الأول.pdf' && box.children[1].textContent.endsWith('.pdf'), 'render: Arabic name, .pdf end kept');

// ---- pages and CSS
const css = fs.readFileSync(path.join(__dirname, '..', 'style.css'), 'utf8');
ok(/\.hero-inner\{[^}]*grid-template-columns:minmax\(0,1fr\) minmax\(0,1fr\)/.test(css) && !/\.hero-inner\{[^}]*grid-template-columns:1fr/.test(css), 'css: hero grid columns can shrink (no blow-out)');
ok(css.includes('.file-name-start{flex:0 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}'), 'css: middle ellipsis');
ok(/\.hero-copy,\.converter-card,[^{]*\{min-width:0\}/.test(css), 'css: flex/grid children min-width 0');
for (const [f, pre] of [['index.html', ''], ['scanned-pdf-to-word.html', ''], ['fr/index.html', '../'], ['ar/index.html', '../']]) {
  const s = fs.readFileSync(path.join(__dirname, '..', f), 'utf8');
  ok(s.indexOf(`<script src="${pre}filename.js" defer></script>`) > -1
     && s.indexOf(`<script src="${pre}filename.js" defer></script>`) < s.indexOf(`<script src="${pre}script.js" defer></script>`), `${f}: filename.js before script.js`);
}
const js = fs.readFileSync(path.join(__dirname, '..', 'script.js'), 'utf8');
ok(js.includes('window.pwFileName.render(document.getElementById("fileName"),e.name)')
   && js.includes('window.pwFileName.docxName(e.name)')
   && js.includes('window.pwFileName.render(document.getElementById("resultFileName"),e.fileName)'), 'script.js uses filename.js (card, download name, success)');

console.log(`\n${passed} passed`);
