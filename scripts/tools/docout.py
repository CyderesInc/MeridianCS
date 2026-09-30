"""Page and print path for the documentation generators.

No generator takes an output path: --print-to-pdf overwrites silently, and that argument once let
`make-brief.py SKILL.md` replace SKILL.md with a PDF.
"""
import os
import shutil
import sys
import tempfile

SKILL = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(SKILL, "scripts"))
import meridian  # noqa: E402

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{title}</title>
<style>{fonts}
{brand}
{prose}</style></head><body>
<div class="masthead">
  <div class="brandrow"><span class="wordmark">{wordmark}</span><span class="product">{product}{badge}</span></div>
  <h1>{title}</h1>
  <div class="meta">{subtitle}</div>
</div>
{body}
<div class="report-footer"><span>{footer}</span><span class="conf">{confidentiality}</span></div>
</body></html>"""


def render(name, *, title, subtitle, prose_css, body, footer, confidentiality, badge=""):
    html = PAGE.format(
        title=meridian._esc(title), subtitle=subtitle, fonts=meridian._font_face_css(),
        brand=meridian._load_css(), prose=prose_css, wordmark=meridian._logo_svg(),
        product=meridian._meridian_svg(), badge=badge, body=body, footer=footer,
        confidentiality=confidentiality)
    out = os.path.join(SKILL, name)
    print_pdf(html, out)
    print("PDF: %s (%d bytes)" % (out, os.path.getsize(out)))


def print_pdf(html, out):
    """Replace `out` only once a new PDF exists, so a failed print cannot pass on the old one."""
    browser = meridian._find_browser()
    if not browser:
        sys.exit("No Chromium browser found; the PDF was not regenerated.")
    with tempfile.TemporaryDirectory() as tmp:
        page, pdf = os.path.join(tmp, "page.html"), os.path.join(tmp, "page.pdf")
        with open(page, "w", encoding="utf-8") as f:
            f.write(html)
        meridian._print_to_pdf(browser, page, pdf)
        if not os.path.exists(pdf):
            sys.exit("%s did not produce a PDF; %s is unchanged." % (os.path.basename(browser), out))
        shutil.move(pdf, out)
