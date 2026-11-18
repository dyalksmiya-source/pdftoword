"""Apply _tools/site-config.json to every HTML page of the site.

Business values (legal name, support email, price, refund days, reply time)
live in ONE place: site-config.json. Every page marks where they appear with
data-config="<key>", for example:

    <span data-config="legal-name">[LEGAL NAME]</span>
    <a data-config="support-email" href="mailto:support@pdf2wordly.com">support@pdf2wordly.com</a>

Usage (from the website/ folder):
    python _tools/apply_site_config.py           # update the pages
    python _tools/apply_site_config.py --check   # only report; exit 1 if a page is out of date

The pages stay plain static HTML (readable without JavaScript, e.g. by Paddle's
reviewers). This folder starts with "_", so GitHub Pages (Jekyll) does not
publish it. It contains no secrets.
"""
import html
import json
import re
import sys
from pathlib import Path

SITE = Path(__file__).resolve().parent.parent
CONFIG = json.loads((Path(__file__).resolve().parent / "site-config.json").read_text(encoding="utf-8"))
ELEMENT = re.compile(r'<(?P<tag>[a-z0-9]+)(?P<attrs>[^>]*\sdata-config="(?P<key>[a-z-]+)"[^>]*)>(?P<inner>[^<]*)</(?P=tag)>')


def apply(text):
    unknown = set()

    def replace(m):
        key = m.group("key")
        if key not in CONFIG:
            unknown.add(key)
            return m.group(0)
        value = html.escape(CONFIG[key], quote=False)
        attrs = m.group("attrs")
        if key == "support-email":
            attrs = re.sub(r'href="mailto:[^"]*"', f'href="mailto:{html.escape(CONFIG[key])}"', attrs)
        return f'<{m.group("tag")}{attrs}>{value}</{m.group("tag")}>'

    return ELEMENT.sub(replace, text), unknown


def main():
    check = "--check" in sys.argv
    changed, total, unknown_keys = [], 0, set()
    for page in sorted(SITE.rglob("*.html")):
        if any(part.startswith((".", "_")) for part in page.relative_to(SITE).parts):
            continue
        original = page.read_bytes().decode("utf-8")
        total += len(ELEMENT.findall(original))
        updated, unknown = apply(original)
        unknown_keys |= unknown
        if updated != original:
            changed.append(page.relative_to(SITE).as_posix())
            if not check:
                page.write_bytes(updated.encode("utf-8"))
    print(f"{total} marked values found; pages {'out of date' if check else 'updated'}: {changed or 'none'}")
    if unknown_keys:
        print("Unknown data-config keys:", sorted(unknown_keys))
        return 1
    return 1 if (check and changed) else 0


if __name__ == "__main__":
    sys.exit(main())
