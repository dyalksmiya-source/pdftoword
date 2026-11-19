"""Technical SEO checks for the static site (seo.json is the inventory).

Run from website/:   python -m unittest discover -s _tests -v
"""
import json
import re
import subprocess
import sys
import unittest
from html.parser import HTMLParser
from pathlib import Path

SITE = Path(__file__).resolve().parent.parent
CFG = json.loads((SITE / "_tools" / "seo.json").read_text(encoding="utf-8"))
SITE_CFG = json.loads((SITE / "_tools" / "site-config.json").read_text(encoding="utf-8"))
BASE = CFG["site"]
PAGES = CFG["pages"]
INDEXABLE = {k: v for k, v in PAGES.items() if v.get("index")}
GUIDES = ["scanned-pdf-to-word.html", "how-to-convert-pdf-to-word.html", "pdf-to-word-without-losing-formatting.html",
          "pdf-to-word-tables-and-business-documents.html", "pdf-to-word-resume.html", "convert-multiple-pdf-files-to-word.html",
          "pdf-to-word-on-mac-and-mobile.html", "pdf-to-word-alternative-to-ilovepdf.html", "pdf-to-word-vs-smallpdf.html",
          "unlock-password-protected-pdf-to-word.html", "pdf-to-word-not-working.html"]


class Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.h1 = 0
        self.hrefs = []
        self.imgs = []
        self.text = []
        self.skip = 0
        self.in_body = False
        self.ids = set()

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if "id" in a:
            self.ids.add(a["id"])
        if tag == "body":
            self.in_body = True
        if tag == "h1":
            self.h1 += 1
        if tag in ("script", "style"):
            self.skip += 1
        if tag == "a" and a.get("href") is not None:
            self.hrefs.append(a["href"])
        if tag == "img":
            self.imgs.append(a)

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip -= 1

    def handle_data(self, data):
        if self.in_body and not self.skip:
            self.text.append(data)


def read(page):
    return (SITE / page).read_text(encoding="utf-8")


def parse(page):
    p = Page()
    p.feed(read(page))
    return p


def head(page):
    s = read(page)
    return s[:s.index("</head>")]


def attr(h, rx):
    m = re.search(rx, h)
    return html_unescape(m.group(1)) if m else None


def html_unescape(s):
    import html
    return html.unescape(s)


def norm(s):
    s = html_unescape(s).lower()
    for a, b in (("أ", "ا"), ("إ", "ا"), ("آ", "ا"), ("ى", "ي"), ("’", "'")):
        s = s.replace(a, b)
    return " ".join(s.split())


def ld_blocks(page):
    out = []
    for b in re.findall(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', read(page), re.S):
        out.append(json.loads(b))
    return out


def ld_items(page):
    items = []
    for j in ld_blocks(page):
        items.extend(j["@graph"] if "@graph" in j else [j])
    return items


class ToolsUpToDate(unittest.TestCase):
    def run_tool(self, name):
        r = subprocess.run([sys.executable, str(SITE / "_tools" / name), "--check"], capture_output=True, text=True, cwd=SITE)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_apply_seo_up_to_date(self):
        self.run_tool("apply_seo.py")

    def test_sitemap_up_to_date(self):
        self.run_tool("build_sitemap.py")

    def test_every_html_page_is_in_the_inventory(self):
        found = {p.relative_to(SITE).as_posix() for p in list(SITE.glob("*.html")) + list(SITE.glob("*/*.html"))
                 if not p.relative_to(SITE).parts[0].startswith(("_", "."))}
        self.assertEqual(found, set(PAGES))


class TitlesDescriptionsH1(unittest.TestCase):
    def test_unique_and_lengths(self):
        titles, descs, h1s = {}, {}, {}
        for page, p in INDEXABLE.items():
            h = head(page)
            title = attr(h, r"<title>(.*?)</title>")
            desc = attr(h, r'<meta name="description" content="([^"]*)"')
            self.assertEqual(title, p["title"], page)
            self.assertEqual(desc, p["description"], page)
            self.assertLessEqual(len(title), 60, f"{page}: title {len(title)} chars")
            self.assertLessEqual(len(desc), 155, f"{page}: description {len(desc)} chars")
            self.assertGreaterEqual(len(desc), 70, f"{page}: description too short")
            self.assertTrue(title.endswith(CFG["brand"]), f"{page}: brand last")
            self.assertEqual(len(re.findall(r"<title>", h)), 1, page)
            self.assertEqual(len(re.findall(r'<meta name="description"', h)), 1, page)
            pg = parse(page)
            self.assertEqual(pg.h1, 1, f"{page}: exactly one H1")
            h1 = norm(re.sub(r"<[^>]+>", "", re.search(r"<h1[^>]*>(.*?)</h1>", read(page), re.S).group(1)))
            titles.setdefault(title, []).append(page)
            descs.setdefault(desc, []).append(page)
            h1s.setdefault(h1, []).append(page)
        for name, seen in (("title", titles), ("description", descs), ("H1", h1s)):
            dupes = {k: v for k, v in seen.items() if len(v) > 1}
            self.assertEqual(dupes, {}, f"duplicate {name}")

    def test_primary_keyword_unique_and_in_title(self):
        primaries = {}
        for page, p in INDEXABLE.items():
            primaries.setdefault(norm(p["primary"]), []).append(page)
            words = [w for w in norm(p["primary"]).split() if len(w) > 2 or w in ("to",)]
            hay = norm(p["title"] + " " + p["description"])
            self.assertTrue(all(w in hay for w in words), f"{page}: primary '{p['primary']}' not in title/description")
        self.assertEqual({k: v for k, v in primaries.items() if len(v) > 1}, {}, "cannibalisation")


class CanonicalHreflang(unittest.TestCase):
    def test_self_canonical_absolute_clean(self):
        for page, p in INDEXABLE.items():
            h = head(page)
            canon = re.findall(r'<link rel="canonical" href="([^"]*)"', h)
            self.assertEqual(canon, [BASE + p["url"]], page)
            self.assertNotIn("index.html", canon[0])
            self.assertTrue(canon[0].startswith("https://pdf2wordly.com/"))

    def test_hreflang_reciprocal(self):
        clusters = {}
        for page, p in INDEXABLE.items():
            alts = dict(re.findall(r'<link rel="alternate" hreflang="([^"]*)" href="([^"]*)"', head(page)))
            if p.get("group"):
                self.assertEqual(set(alts), {"en", "fr", "ar", "x-default"}, page)
                self.assertEqual(alts[p["lang"]], BASE + p["url"], f"{page}: hreflang includes itself")
                clusters.setdefault(p["group"], []).append((page, alts))
            else:
                self.assertEqual(alts, {}, f"{page}: no translations, no hreflang")
        for group, members in clusters.items():
            first = members[0][1]
            for page, alts in members:
                self.assertEqual(alts, first, f"{group}: {page} not reciprocal")
            for lang, url in first.items():
                if lang == "x-default":
                    continue
                target = next(k for k, v in INDEXABLE.items() if BASE + v["url"] == url)
                self.assertEqual(PAGES[target]["lang"], lang)

    def test_html_lang_and_dir(self):
        for page, p in PAGES.items():
            m = re.search(r'<html lang="([a-z]+)"( dir="rtl")?', read(page))
            self.assertEqual(m.group(1), p["lang"], page)
            self.assertEqual(bool(m.group(2)), p["lang"] == "ar", f"{page}: dir")


class Robots(unittest.TestCase):
    def test_noindex_pages(self):
        must_noindex = {"account.html", "login.html", "forgot-password.html", "reset-password.html", "404.html",
                        "fr/login.html", "fr/forgot-password.html", "fr/reset-password.html",
                        "ar/login.html", "ar/forgot-password.html", "ar/reset-password.html"}
        for page in must_noindex:
            self.assertFalse(PAGES[page]["index"], page)
            h = head(page)
            self.assertEqual(re.findall(r'<meta name="robots" content="([^"]*)"', h), ["noindex, follow"], page)
            self.assertNotIn('rel="canonical"', h, page)
        for page in INDEXABLE:
            self.assertEqual(re.findall(r'<meta name="robots" content="([^"]*)"', head(page)), ["index, follow, max-image-preview:large"], page)
        for page in ("delete-account.html", "fr/delete-account.html", "ar/delete-account.html"):
            self.assertIn(page, INDEXABLE, "Google Play help page stays indexable")

    def test_robots_txt(self):
        r = (SITE / "robots.txt").read_text(encoding="utf-8")
        self.assertIn("Allow: /", r)
        self.assertIn("Sitemap: https://pdf2wordly.com/sitemap.xml", r)
        for line in r.splitlines():
            if line.lower().startswith("disallow:"):
                self.assertIn(line.split(":", 1)[1].strip(), ("/api/",), "nothing important blocked")


class Sitemap(unittest.TestCase):
    def test_matches_inventory(self):
        import xml.dom.minidom
        sm = (SITE / "sitemap.xml").read_text(encoding="utf-8")
        xml.dom.minidom.parseString(sm)
        locs = re.findall(r"<loc>(.*?)</loc>", sm)
        self.assertEqual(sorted(locs), sorted(BASE + p["url"] for p in INDEXABLE.values()))
        self.assertEqual(len(locs), len(set(locs)))
        self.assertEqual(len(re.findall(r"<lastmod>\d{4}-\d{2}-\d{2}</lastmod>", sm)), len(locs))
        for page, p in PAGES.items():
            if not p.get("index"):
                self.assertNotIn(p.get("url", page), [l.replace(BASE, "") for l in locs])


class Links(unittest.TestCase):
    def test_no_internal_link_to_index_html(self):
        for page in PAGES:
            bad = [h for h in parse(page).hrefs if re.search(r"(^|/)index\.html", h)]
            self.assertEqual(bad, [], page)

    def test_internal_links_resolve(self):
        pages = {p: parse(p) for p in PAGES}
        for page, pg in pages.items():
            for h in pg.hrefs:
                if h.startswith("https://pdf2wordly.com"):
                    h = h[len("https://pdf2wordly.com"):] or "/"
                elif re.match(r"^[a-z]+:", h) or h.startswith("//") or h == "#":
                    continue
                path, _, frag = h.partition("#")
                if not path:
                    target = page
                elif path.startswith("/"):
                    target = path.lstrip("/") or "index.html"
                else:
                    target = (Path(page).parent / path).as_posix()
                if target.endswith("/") or (SITE / target).is_dir():
                    target = target.rstrip("/") + "/index.html"
                target = target.lstrip("./") if target.startswith("./") else target
                self.assertTrue((SITE / target).exists(), f"{page}: {h}")
                if frag and target in pages:
                    self.assertIn(frag, pages[target].ids, f"{page}: {h}")

    def test_guides_link_converter_and_related(self):
        for g in GUIDES:
            s = read(g)
            block = re.search(r"<!-- related:start.*?<!-- related:end -->", s, re.S)
            self.assertIsNotNone(block, g)
            links = re.findall(r'href="/([a-z-]+\.html)"', block.group(0))
            self.assertEqual(len(links), 3, g)
            self.assertNotIn(g, links)
            self.assertTrue(all(l in GUIDES for l in links), g)
            self.assertIn('href="/#converter"', s, f"{g}: links to the converter")

    def test_home_links_main_guides(self):
        for page in ("index.html", "fr/index.html", "ar/index.html"):
            s = read(page)
            main = s[s.index("<main"):s.index("</main>")]
            self.assertGreaterEqual(len(set(re.findall(r'href="/([a-z-]+\.html)"', main)) & set(GUIDES)), 5, page)

    def test_footer_clean(self):
        for page in PAGES:
            s = read(page)
            m = re.search(r"<!-- footer:start.*?<!-- footer:end -->", s, re.S)
            if 'class="footer-inner"' not in s:
                continue
            self.assertIsNotNone(m, page)
            hrefs = re.findall(r'href="([^"]+)"', m.group(0))
            self.assertEqual(len(hrefs), len(set(hrefs)), f"{page}: duplicate footer links")
            self.assertEqual(re.findall(r"<h3>([^<]+)</h3>", m.group(0))[0], {"en": "Tools", "fr": "Outils", "ar": "أدوات"}[PAGES[page]["lang"]])


class StructuredData(unittest.TestCase):
    def test_valid_json_and_types(self):
        for page in PAGES:
            for j in ld_blocks(page):
                self.assertEqual(j.get("@context"), "https://schema.org", page)
        for page in INDEXABLE:
            types = [i["@type"] for i in ld_items(page)]
            self.assertIn("Organization", types, page)
            self.assertIn("WebSite", types, page)
            self.assertEqual(types.count("Organization"), 1, page)
        for page in ("index.html", "fr/index.html", "ar/index.html", "scanned-pdf-to-word.html"):
            app = next(i for i in ld_items(page) if i["@type"] == "WebApplication")
            offers = {o["price"]: o for o in app["offers"]}
            self.assertEqual(set(offers), {"0", SITE_CFG["pro-price"].lstrip("$")}, page)
            self.assertEqual({o["priceCurrency"] for o in app["offers"]}, {"USD"})
            self.assertEqual(offers[SITE_CFG["pro-price"].lstrip("$")]["priceSpecification"]["unitCode"], "MON")
            self.assertEqual(app["inLanguage"], PAGES[page]["lang"])
        logo = next(i for i in ld_items("index.html") if i["@type"] == "Organization")["logo"]["url"]
        self.assertTrue((SITE / logo.replace(BASE + "/", "")).exists())

    def test_faq_only_where_visible(self):
        for page in PAGES:
            text = norm(" ".join(parse(page).text))
            for item in ld_items(page):
                if item["@type"] != "FAQPage":
                    continue
                for q in item["mainEntity"]:
                    self.assertIn(norm(q["name"]), text, f"{page}: FAQ question not visible: {q['name']}")
                    ans = norm(re.sub(r"<[^>]+>", "", q["acceptedAnswer"]["text"]))
                    self.assertIn(ans[:60], text, f"{page}: FAQ answer not visible: {ans[:60]}")

    def test_breadcrumbs_on_guides(self):
        for g in GUIDES:
            bc = [i for i in ld_items(g) if i["@type"] == "BreadcrumbList"]
            self.assertEqual(len(bc), 1, g)
            items = bc[0]["itemListElement"]
            self.assertEqual(items[0]["item"], BASE + "/", g)
            self.assertEqual(items[-1]["item"], BASE + PAGES[g]["url"], g)


class Social(unittest.TestCase):
    def test_open_graph_and_twitter(self):
        from PIL import Image
        img = Image.open(SITE / "og-image.png")
        self.assertEqual(img.size, (1200, 630))
        for page, p in INDEXABLE.items():
            h = head(page)
            og = dict(re.findall(r'<meta property="(og:[a-z:_]+)" content="([^"]*)"', h))
            for k in ("og:type", "og:title", "og:description", "og:url", "og:image", "og:image:width", "og:image:height", "og:locale", "og:site_name"):
                self.assertIn(k, og, f"{page}: {k}")
            self.assertEqual(og["og:url"], BASE + p["url"], page)
            self.assertEqual(og["og:locale"], CFG["locales"][p["lang"]])
            alternates = re.findall(r'<meta property="og:locale:alternate" content="([^"]*)"', h)
            self.assertEqual(len(alternates), 2 if p.get("group") else 0, page)
            tw = dict(re.findall(r'<meta name="(twitter:[a-z:]+)" content="([^"]*)"', h))
            self.assertEqual(tw.get("twitter:card"), "summary_large_image", page)
            self.assertEqual(tw.get("twitter:image"), CFG["image"]["url"], page)


class Speed(unittest.TestCase):
    def test_images_sized_and_lazy(self):
        for page in PAGES:
            for i, im in enumerate(parse(page).imgs):
                self.assertIn("width", im, page)
                self.assertIn("height", im, page)
                if i > 0:
                    self.assertEqual(im.get("loading"), "lazy", page)

    def test_font_preload_and_no_stale_hosts(self):
        for page in PAGES:
            h = head(page)
            if "fonts.googleapis.com/css2" in h:
                self.assertIn('<link rel="preload" as="style" href="https://fonts.googleapis.com/css2', h, page)
            self.assertNotIn("pdftoword-production-94af.up.railway.app", read(page), page)

    def test_billing_only_on_pricing_pages_and_paddle_lazy(self):
        for page in PAGES:
            has = bool(re.search(r'<script[^>]+src="[^"]*billing\.js"', read(page)))
            self.assertEqual(has, page in ("index.html", "fr/index.html", "ar/index.html"), page)
            self.assertNotIn("cdn.paddle.com", read(page), f"{page}: Paddle.js only through billing.js")
        js = (SITE / "billing.js").read_text(encoding="utf-8")
        self.assertIn("waitUntilNeeded: function (button) { return whenNeeded(document, window, button); }", js)


class NotFound(unittest.TestCase):
    def test_404(self):
        s = read("404.html")
        self.assertIn('<meta name="robots" content="noindex, follow">', s)
        hrefs = parse("404.html").hrefs
        for h in ("/#converter", "/fr/", "/ar/", "/scanned-pdf-to-word.html", "/how-to-convert-pdf-to-word.html"):
            self.assertIn(h, hrefs)
        self.assertTrue(all(h.startswith("/") or h.startswith("http") for h in hrefs), "root-relative links only")
        self.assertIn('href="/style.css"', s)
        self.assertNotIn("supabase", s)


if __name__ == "__main__":
    unittest.main()
