"""Build sitemap.xml from _tools/seo.json (indexable pages only).

- One <url> per page with "index": true, in seo.json order.
- <lastmod>: the date of the page's last git commit; today if the page has
  uncommitted changes (it is about to be published).
- xhtml:link hreflang alternates (+ x-default) for pages with translations.

Usage (from website/):
    python _tools/build_sitemap.py           # write sitemap.xml
    python _tools/build_sitemap.py --check   # exit 1 if sitemap.xml is stale
"""
import datetime
import json
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

SITE = Path(__file__).resolve().parent.parent
CFG = json.loads((Path(__file__).resolve().parent / "seo.json").read_text(encoding="utf-8"))
BASE = CFG["site"]


def git(*args):
    return subprocess.run(["git", "-C", str(SITE), *args], capture_output=True, text=True).stdout.strip()


def lastmod(page):
    if git("status", "--porcelain", "--", page):
        return datetime.date.today().isoformat()
    return git("log", "-1", "--format=%cs", "--", page) or datetime.date.today().isoformat()


def build():
    pages = CFG["pages"]
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:xhtml="http://www.w3.org/1999/xhtml">']
    for page, p in pages.items():
        if not p.get("index"):
            continue
        out.append("  <url>")
        out.append(f"    <loc>{escape(BASE + p['url'])}</loc>")
        out.append(f"    <lastmod>{lastmod(page)}</lastmod>")
        group = [k for k, v in pages.items() if v.get("index") and v.get("group") and v.get("group") == p.get("group")]
        if group:
            order = ["en", "fr", "ar"]
            for k in sorted(group, key=lambda k: order.index(pages[k]["lang"])):
                out.append(f'    <xhtml:link rel="alternate" hreflang="{pages[k]["lang"]}" href="{escape(BASE + pages[k]["url"])}"/>')
            xd = next(k for k in group if pages[k]["lang"] == CFG["x_default"])
            out.append(f'    <xhtml:link rel="alternate" hreflang="x-default" href="{escape(BASE + pages[xd]["url"])}"/>')
        out.append("  </url>")
    out.append("</urlset>")
    return "\n".join(out) + "\n"


def main():
    new = build()
    target = SITE / "sitemap.xml"
    old = target.read_text(encoding="utf-8") if target.exists() else ""
    if "--check" in sys.argv:
        print("sitemap.xml", "up to date" if new == old else "OUT OF DATE")
        return 0 if new == old else 1
    target.write_text(new, encoding="utf-8", newline="\n")
    print(f"sitemap.xml written: {new.count('<url>')} URLs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
