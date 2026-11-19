"""Apply _tools/seo.json to the HTML pages (technical SEO, one source).

What it writes (always between markers, so re-running is safe):
  - <head>: title, meta description, robots, absolute self-canonical,
    reciprocal hreflang (en/fr/ar + x-default) for translated pages, Open
    Graph + Twitter card, font stylesheet preload.          <!-- seo:start/end -->
  - structured data: Organization + WebSite on every indexable page, and
    WebApplication with the Free and Pro offers (Pro price from
    site-config.json) on the converter pages.        data-seo="site" / "app"
  - internal links: index.html -> clean URLs (/, /fr/, /ar/).
  - footer columns (one standard footer per language).  <!-- footer:start/end -->
  - "Related guides" on every guide; guides block on the home pages.

Old, hand-written copies of these tags are removed. FAQPage, HowTo,
BreadcrumbList, AboutPage and ContactPage blocks are left as they are.

Usage (from website/):
    python _tools/apply_seo.py           # update the pages
    python _tools/apply_seo.py --check   # exit 1 if a page is out of date
"""
import html
import json
import re
import sys
from pathlib import Path

SITE = Path(__file__).resolve().parent.parent
TOOLS = Path(__file__).resolve().parent
CFG = json.loads((TOOLS / "seo.json").read_text(encoding="utf-8"))
SITE_CFG = json.loads((TOOLS / "site-config.json").read_text(encoding="utf-8"))
BASE = CFG["site"]
FONTS_CSS = re.compile(r'<link href="(https://fonts\.googleapis\.com/css2[^"]+)" rel="stylesheet">')

GUIDES = [
    # file, en label, fr label, ar label
    ("how-to-convert-pdf-to-word.html", "How to Convert PDF to Word", "Comment convertir un PDF en Word", "كيفية تحويل PDF إلى Word"),
    ("scanned-pdf-to-word.html", "Scanned PDF to Word (OCR)", "PDF scanné vers Word (OCR)", "تحويل PDF الممسوح ضوئيًا إلى Word"),
    ("pdf-to-word-without-losing-formatting.html", "Keep Your Formatting", "Garder la mise en forme", "الحفاظ على التنسيق"),
    ("pdf-to-word-tables-and-business-documents.html", "Tables, Invoices & Reports", "Tableaux, factures et rapports", "الجداول والفواتير والتقارير"),
    ("pdf-to-word-resume.html", "PDF Resume to Word", "CV PDF vers Word", "السيرة الذاتية من PDF إلى Word"),
    ("convert-multiple-pdf-files-to-word.html", "Convert Multiple PDF Files", "Convertir plusieurs PDF", "تحويل عدة ملفات PDF"),
    ("pdf-to-word-on-mac-and-mobile.html", "On Mac, iPhone & Android", "Sur Mac, iPhone et Android", "على Mac وiPhone وAndroid"),
    ("unlock-password-protected-pdf-to-word.html", "Password-Protected PDFs", "PDF protégés par mot de passe", "ملفات PDF المحمية بكلمة مرور"),
    ("pdf-to-word-not-working.html", "Conversion Not Working?", "La conversion échoue ?", "التحويل لا يعمل؟"),
]
COMPARE = [
    ("pdf-to-word-alternative-to-ilovepdf.html", "iLovePDF Alternative"),
    ("pdf-to-word-vs-smallpdf.html", "Smallpdf Alternative"),
]
LABEL = {g[0]: g[1] for g in GUIDES} | {c[0]: c[1] for c in COMPARE}
RELATED = {
    "scanned-pdf-to-word.html": ["how-to-convert-pdf-to-word.html", "pdf-to-word-without-losing-formatting.html", "pdf-to-word-tables-and-business-documents.html"],
    "how-to-convert-pdf-to-word.html": ["scanned-pdf-to-word.html", "pdf-to-word-without-losing-formatting.html", "unlock-password-protected-pdf-to-word.html"],
    "pdf-to-word-without-losing-formatting.html": ["pdf-to-word-tables-and-business-documents.html", "pdf-to-word-resume.html", "scanned-pdf-to-word.html"],
    "pdf-to-word-tables-and-business-documents.html": ["pdf-to-word-without-losing-formatting.html", "scanned-pdf-to-word.html", "convert-multiple-pdf-files-to-word.html"],
    "pdf-to-word-resume.html": ["pdf-to-word-without-losing-formatting.html", "how-to-convert-pdf-to-word.html", "pdf-to-word-on-mac-and-mobile.html"],
    "convert-multiple-pdf-files-to-word.html": ["how-to-convert-pdf-to-word.html", "pdf-to-word-not-working.html", "pdf-to-word-tables-and-business-documents.html"],
    "pdf-to-word-on-mac-and-mobile.html": ["how-to-convert-pdf-to-word.html", "scanned-pdf-to-word.html", "pdf-to-word-resume.html"],
    "pdf-to-word-alternative-to-ilovepdf.html": ["pdf-to-word-vs-smallpdf.html", "scanned-pdf-to-word.html", "how-to-convert-pdf-to-word.html"],
    "pdf-to-word-vs-smallpdf.html": ["pdf-to-word-alternative-to-ilovepdf.html", "scanned-pdf-to-word.html", "pdf-to-word-without-losing-formatting.html"],
    "unlock-password-protected-pdf-to-word.html": ["pdf-to-word-not-working.html", "how-to-convert-pdf-to-word.html", "scanned-pdf-to-word.html"],
    "pdf-to-word-not-working.html": ["unlock-password-protected-pdf-to-word.html", "scanned-pdf-to-word.html", "convert-multiple-pdf-files-to-word.html"],
}
APP_PAGES = {"index.html", "fr/index.html", "ar/index.html", "scanned-pdf-to-word.html"}
HOME = {"en": "/", "fr": "/fr/", "ar": "/ar/"}
T = {
    "en": {"tools": "Tools", "guides": "Guides", "more": "More", "about": "About", "contact": "Contact",
           "convert": "PDF to Word", "related": "Related guides", "cta": "Convert a PDF to Word now",
           "home_guides": "Guides", "home_guides_sub": "Answers to the most common PDF to Word questions.", "in_en": ""},
    "fr": {"tools": "Outils", "guides": "Guides (en anglais)", "more": "Plus", "about": "À propos", "contact": "Contact",
           "convert": "Convertir PDF en Word", "related": "Guides associés", "cta": "Convertir un PDF en Word",
           "home_guides": "Guides", "home_guides_sub": "Nos guides détaillés (en anglais) pour les cas les plus fréquents.", "in_en": " (en anglais)"},
    "ar": {"tools": "أدوات", "guides": "أدلة (بالإنجليزية)", "more": "المزيد", "about": "من نحن", "contact": "اتصل بنا",
           "convert": "تحويل PDF إلى Word", "related": "أدلة ذات صلة", "cta": "حوّل ملف PDF إلى Word الآن",
           "home_guides": "أدلة", "home_guides_sub": "أدلة مفصّلة (بالإنجليزية) لأكثر الحالات شيوعًا.", "in_en": " (بالإنجليزية)"},
}


def esc(s):
    return html.escape(s, quote=True)


def abs_url(path):
    return BASE + path


def lang_of(page):
    return CFG["pages"][page]["lang"]


# ---------------------------------------------------------------- head block
def head_block(page, s):
    p = CFG["pages"][page]
    lines = ["<!-- seo:start: generated by _tools/apply_seo.py from _tools/seo.json; edit seo.json, not this block -->",
             f"<title>{esc(p['title'])}</title>",
             f'<meta name="description" content="{esc(p["description"])}">']
    if p["index"]:
        url = abs_url(p["url"])
        lines += ['<meta name="robots" content="index, follow, max-image-preview:large">',
                  f'<link rel="canonical" href="{url}">']
        group = [k for k, v in CFG["pages"].items() if v.get("group") and v.get("group") == p.get("group") and v["index"]]
        if group:
            for k in sorted(group, key=lambda k: ["en", "fr", "ar"].index(lang_of(k))):
                lines.append(f'<link rel="alternate" hreflang="{lang_of(k)}" href="{abs_url(CFG["pages"][k]["url"])}">')
            xd = next(k for k in group if lang_of(k) == CFG["x_default"])
            lines.append(f'<link rel="alternate" hreflang="x-default" href="{abs_url(CFG["pages"][xd]["url"])}">')
        img = CFG["image"]
        lang = p["lang"]
        lines += [f'<meta property="og:type" content="{p["type"]}">',
                  '<meta property="og:site_name" content="PDF2Wordly">',
                  f'<meta property="og:title" content="{esc(p["title"])}">',
                  f'<meta property="og:description" content="{esc(p["description"])}">',
                  f'<meta property="og:url" content="{url}">',
                  f'<meta property="og:image" content="{img["url"]}">',
                  f'<meta property="og:image:width" content="{img["width"]}">',
                  f'<meta property="og:image:height" content="{img["height"]}">',
                  f'<meta property="og:image:alt" content="{esc(img["alt"][lang])}">',
                  f'<meta property="og:locale" content="{CFG["locales"][lang]}">']
        for k in group:
            if k != page:
                lines.append(f'<meta property="og:locale:alternate" content="{CFG["locales"][lang_of(k)]}">')
        lines += ['<meta name="twitter:card" content="summary_large_image">',
                  f'<meta name="twitter:title" content="{esc(p["title"])}">',
                  f'<meta name="twitter:description" content="{esc(p["description"])}">',
                  f'<meta name="twitter:image" content="{img["url"]}">',
                  f'<meta name="twitter:image:alt" content="{esc(img["alt"][lang])}">']
    else:
        lines.append('<meta name="robots" content="noindex, follow">')
    m = FONTS_CSS.search(s)
    if m:
        lines.append(f'<link rel="preload" as="style" href="{m.group(1)}">')
    lines.append("<!-- seo:end -->")
    return "\n".join(lines)


HEAD_JUNK = [
    r"\n?<!-- seo:start.*?<!-- seo:end -->",
    r"\n?[ \t]*<title>.*?</title>",
    r'\n?[ \t]*<meta name="description"[^>]*>',
    r'\n?[ \t]*<meta name="robots"[^>]*>',
    r'\n?[ \t]*<link rel="canonical"[^>]*>',
    r'\n?[ \t]*<link rel="alternate" hreflang="[^"]*"[^>]*>',
    r'\n?[ \t]*<meta property="og:[^"]*"[^>]*>',
    r'\n?[ \t]*<meta name="twitter:[^"]*"[^>]*>',
    r'\n?[ \t]*<link rel="preload" as="style" href="https://fonts\.googleapis\.com[^"]*">',
    r'\n?[ \t]*<link rel="dns-prefetch" href="https://pdftoword-production-94af\.up\.railway\.app">',
    r"\n?[ \t]*<!-- (Open Graph|Twitter|Hreflang[^>]*|Canonical[^>]*) -->",
]


def apply_head(page, s):
    i = s.index("</head>")
    head, rest = s[:i], s[i:]
    for rx in HEAD_JUNK:
        head = re.sub(rx, "", head, flags=re.S)
    anchor = re.search(r'<meta name="theme-color"[^>]*>|<meta name="viewport"[^>]*>', head)
    assert anchor, page
    after = head[anchor.end():]
    if not after.startswith("\n"):
        after = "\n" + after
    head = head[:anchor.end()] + "\n" + head_block(page, s) + after
    head = re.sub(r"\n{3,}", "\n\n", head)
    return head + rest


# ---------------------------------------------------------------- structured data
def ld(obj, key):
    return f'<script type="application/ld+json" data-seo="{key}">\n' + json.dumps(obj, ensure_ascii=False, indent=2) + "\n</script>"


def site_graph(lang):
    return {"@context": "https://schema.org", "@graph": [
        {"@type": "Organization", "@id": BASE + "/#organization", "name": "PDF2Wordly", "url": BASE + "/",
         "logo": {"@type": "ImageObject", "url": BASE + "/paddle-icon.png", "width": 512, "height": 512},
         "email": SITE_CFG["support-email"]},
        {"@type": "WebSite", "@id": BASE + "/#website", "name": "PDF2Wordly", "url": BASE + "/",
         "inLanguage": ["en", "fr", "ar"], "publisher": {"@id": BASE + "/#organization"}},
    ]}


APP_TEXT = {
    "en": ("PDF2Wordly – PDF to Word Converter", "Convert PDF files, including scanned PDFs with OCR, into editable Word documents online.", "Free", "Pro (monthly)"),
    "fr": ("PDF2Wordly – Convertisseur PDF en Word", "Convertissez vos PDF, y compris les PDF scannés grâce à l’OCR, en documents Word modifiables en ligne.", "Gratuit", "Pro (mensuel)"),
    "ar": ("PDF2Wordly – محوّل PDF إلى Word", "حوّل ملفات PDF، بما فيها الملفات الممسوحة ضوئيًا عبر التعرّف الضوئي على النصوص، إلى مستندات Word قابلة للتعديل أونلاين.", "مجاني", "Pro (شهري)"),
}


def app_graph(page):
    p = CFG["pages"][page]
    name, desc, free, pro = APP_TEXT[p["lang"]]
    price = SITE_CFG["pro-price"].lstrip("$")
    home = abs_url(HOME[p["lang"]])
    return {"@context": "https://schema.org", "@type": "WebApplication",
            "name": name, "url": abs_url(p["url"]), "description": desc, "inLanguage": p["lang"],
            "applicationCategory": "BusinessApplication", "operatingSystem": "Any (web browser)",
            "browserRequirements": "Requires JavaScript and a modern browser.",
            "publisher": {"@id": BASE + "/#organization"},
            "offers": [
                {"@type": "Offer", "name": free, "price": "0", "priceCurrency": "USD", "url": home + "#pricing"},
                {"@type": "Offer", "name": pro, "price": price, "priceCurrency": "USD", "url": home + "#pricing",
                 "priceSpecification": {"@type": "UnitPriceSpecification", "price": price, "priceCurrency": "USD",
                                        "unitCode": "MON", "valueAddedTaxIncluded": True,
                                        "referenceQuantity": {"@type": "QuantitativeValue", "value": 1, "unitCode": "MON"}}},
            ]}


def apply_ld(page, s):
    p = CFG["pages"][page]
    # drop old hand-written Organization / WebSite / SoftwareApplication blocks and our own
    def keep(m):
        attrs, body = m.group(1), m.group(2)
        if "data-seo=" in attrs:
            return ""
        try:
            j = json.loads(body)
        except ValueError:
            return m.group(0)
        if isinstance(j, dict) and j.get("@type") in ("Organization", "WebSite", "SoftwareApplication", "WebApplication"):
            return ""
        return m.group(0)
    s = re.sub(r'\n?(?:<!-- Structured data[^>]*-->\n)?<script type="application/ld\+json"([^>]*)>(.*?)</script>', keep, s, flags=re.S)
    s = re.sub(r"\n?<!-- Structured data[^>]*-->(?=\n\s*\n|\n<)", "", s)
    i = s.index("</head>")
    head, rest = s[:i].rstrip(), s[i:]
    if p["index"]:
        blocks = [ld(site_graph(p["lang"]), "site")]
        if page in APP_PAGES:
            blocks.append(ld(app_graph(page), "app"))
        head += "\n" + "\n".join(blocks)
    # one blank line at most between head tags, so a second run changes nothing
    head = re.sub(r"\n(?:[ \t]*\n){2,}", "\n\n", head)
    return head + "\n" + rest


# ---------------------------------------------------------------- clean links
def clean_links(page, s):
    lang = lang_of(page) if "/" in page else "en"
    home = "/" if "/" not in page else f"/{page.split('/')[0]}/"
    s = re.sub(r'href="https://pdf2wordly\.com/index\.html(#[^"]*)?"', lambda m: f'href="https://pdf2wordly.com/{m.group(1) or ""}"', s)
    s = re.sub(r'href="\.\./index\.html(#[^"]*)?"', lambda m: f'href="/{m.group(1) or ""}"', s)
    s = re.sub(r'href="(?:\./)?index\.html(#[^"]*)?"', lambda m: f'href="{home}{m.group(1) or ""}"', s)
    return s


# ---------------------------------------------------------------- footer
def footer(lang):
    t = T[lang]
    a = lambda href, label, extra="": f'<li><a href="{href}"{extra}>{label}</a></li>'
    en = ' hreflang="en"' if lang != "en" else ""
    col = lambda title, items: (f'    <div class="footer-col">\n      <h3>{title}</h3>\n      <ul>\n        '
                                + "\n        ".join(items) + "\n      </ul>\n    </div>")
    tools = [a(HOME[lang] + "#converter", t["convert"])]
    tools.append(a("/scanned-pdf-to-word.html", {"en": "Scanned PDF to Word (OCR)", "fr": "PDF scanné vers Word (OCR)", "ar": "PDF ممسوح ضوئيًا إلى Word (OCR)"}[lang], en))
    for other, label, attrs in (("en", "PDF to Word (English)", ' lang="en" hreflang="en"'),
                                ("fr", "Convertir PDF en Word (français)", ' lang="fr" hreflang="fr"'),
                                ("ar", "تحويل PDF إلى Word (العربية)", ' lang="ar" hreflang="ar" dir="rtl"')):
        if other != lang:
            tools.append(a(HOME[other], label, attrs))
    idx = {"en": 1, "fr": 2, "ar": 3}[lang]
    # the scanned-PDF page is already under Tools: list every page once
    guides = [a("/" + g[0], esc(g[idx]), en) for g in GUIDES if g[0] != "scanned-pdf-to-word.html"]
    more = [a("/" + c[0], esc(c[1]), en) for c in COMPARE] if lang == "en" else []
    more += [a("/about.html", t["about"], en), a("/contact.html", t["contact"], en)]
    return ("<!-- footer:start: generated by _tools/apply_seo.py -->\n"
            + "\n".join([col(t["tools"], tools), col(t["guides"], guides), col(t["more"], more)])
            + "\n    <!-- footer:end -->")


def apply_footer(page, s):
    if 'class="footer-inner"' not in s:
        return s
    lang = lang_of(page)
    s = re.sub(r"\n?[ \t]*<!-- footer:start.*?<!-- footer:end -->", "", s, flags=re.S)
    start = s.index('<div class="footer-inner">')
    brand_end = s.index("</div>", s.index('<div class="footer-brand">', start)) + len("</div>")
    bottom = s.index('<div class="footer-bottom">', start)
    inner_end = s.rindex("</div>", brand_end, bottom)  # closes footer-inner
    return s[:brand_end] + "\n\n    " + footer(lang) + "\n  " + s[inner_end:]


# ---------------------------------------------------------------- related guides / home guides
def related_block(page):
    items = "\n".join(f'        <li><a href="/{g}">{esc(LABEL[g])}</a></li>' for g in RELATED[page])
    return ("<!-- related:start: generated by _tools/apply_seo.py -->\n"
            '<section class="related-guides" aria-labelledby="related-title">\n'
            '  <div class="section-inner">\n'
            f'    <h2 id="related-title">{T["en"]["related"]}</h2>\n'
            f'    <ul class="related-list">\n{items}\n    </ul>\n'
            f'    <p class="related-cta"><a class="btn btn-primary" href="/#converter">{T["en"]["cta"]}</a></p>\n'
            "  </div>\n</section>\n<!-- related:end -->")


HOME_GUIDES = ["scanned-pdf-to-word.html", "how-to-convert-pdf-to-word.html", "pdf-to-word-without-losing-formatting.html",
               "pdf-to-word-tables-and-business-documents.html", "unlock-password-protected-pdf-to-word.html", "pdf-to-word-not-working.html"]


def home_guides_block(lang):
    t = T[lang]
    idx = {"en": 1, "fr": 2, "ar": 3}[lang]
    labels = {g[0]: g[idx] for g in GUIDES}
    en = ' hreflang="en"' if lang != "en" else ""
    items = "\n".join(f'        <li><a href="/{g}"{en}>{esc(labels[g])}</a></li>' for g in HOME_GUIDES)
    return ("<!-- home-guides:start: generated by _tools/apply_seo.py -->\n"
            '<section class="home-guides" id="guides" aria-labelledby="guides-title">\n'
            '  <div class="section-inner">\n'
            f'    <h2 id="guides-title">{t["home_guides"]}</h2>\n'
            f'    <p class="home-guides-sub">{t["home_guides_sub"]}</p>\n'
            f'    <ul class="related-list">\n{items}\n    </ul>\n'
            "  </div>\n</section>\n<!-- home-guides:end -->")


def apply_blocks(page, s):
    s = re.sub(r"\n?<!-- related:start.*?<!-- related:end -->", "", s, flags=re.S)
    s = re.sub(r"<!-- home-guides:start.*?<!-- home-guides:end -->\s*", "", s, flags=re.S)
    if page in RELATED:
        i = s.index("</main>")
        s = s[:i].rstrip() + "\n\n" + related_block(page) + "\n\n" + s[i:]
    if page in ("index.html", "fr/index.html", "ar/index.html"):
        anchor = "<!-- ============ FAQ ============ -->"
        assert s.count(anchor) == 1, page
        s = s.replace(anchor, home_guides_block(lang_of(page)) + "\n\n  " + anchor)
    return s


def render(page, s):
    s = clean_links(page, s)
    s = apply_head(page, s)
    s = apply_ld(page, s)
    s = apply_footer(page, s)
    s = apply_blocks(page, s)
    return s


def main():
    check = "--check" in sys.argv
    stale = []
    pages = [p for p in CFG["pages"] if (SITE / p).exists()]
    missing = [p for p in CFG["pages"] if not (SITE / p).exists()]
    unknown = sorted(str(p.relative_to(SITE).as_posix()) for p in list(SITE.glob("*.html")) + list(SITE.glob("*/*.html"))
                     if not p.relative_to(SITE).parts[0].startswith(("_", ".")) and p.relative_to(SITE).as_posix() not in CFG["pages"])
    for page in pages:
        f = SITE / page
        old = f.read_bytes().decode("utf-8")
        new = render(page, old)
        if new != old:
            stale.append(page)
            if not check:
                f.write_bytes(new.encode("utf-8"))
    print(f"{len(pages)} pages; {'out of date' if check else 'updated'}: {stale or 'none'}")
    if missing:
        print("in seo.json but missing:", missing)
    if unknown:
        print("HTML pages not in seo.json:", unknown)
    return 1 if (check and stale) or unknown else 0


if __name__ == "__main__":
    sys.exit(main())
