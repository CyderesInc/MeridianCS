"""Report themes, phase 1 (design/report-themes.md): the slot loader, the default theme, the bundled
Cyderes theme, and the checks that stop a theme hiding what a report says.

Most tests build a throwaway assets folder holding the real report-base.css and a theme of their
own, so they run the same in the internal tree and the public one (which bundles no theme).
"""
import json
import os
import re
import shutil

import pytest

from test_connect import check, load_meridian


@pytest.fixture(scope="module")
def m():
    return load_meridian()


@pytest.fixture
def assets(m, monkeypatch, tmp_path):
    """An assets folder with the real layout and no theme; `install(theme, files)` bundles one."""
    shutil.copy(os.path.join(m._assets_dir(), "report-base.css"), tmp_path / "report-base.css")
    monkeypatch.setattr(m, "_assets_dir", lambda: str(tmp_path))
    m._THEME_CACHE.pop(str(tmp_path), None)

    def install(theme, files=None):
        d = tmp_path / "themes" / "t"
        d.mkdir(parents=True, exist_ok=True)
        (d / "theme.json").write_text(json.dumps(theme), encoding="utf-8")
        for name, body in (files or {}).items():
            (d / name).write_text(body, encoding="utf-8")
        m._THEME_CACHE.pop(str(tmp_path), None)
        return m._active_theme()

    yield install
    m._THEME_CACHE.pop(str(tmp_path), None)


def _base_css(m):
    with open(os.path.join(m._assets_dir(), "report-base.css"), encoding="utf-8") as f:
        return f.read()


# Text colours that carry no information, or sit on a fixed severity surface. Anything else marked
# `on: -` would be a text rule quietly opting out of the readability check.
EXEMPT = {".kev.hot", "ul.insights li::marker", ".callout.unstable", ".callout.unstable strong", ".toc .sep"}


def test_report_base_annotations(m):
    """Every text colour in the layout names its background, so the contrast pairs are derived from
    the stylesheet and a new text rule cannot escape the check (decision 4)."""
    css = re.sub(r"/\*(?!\s*on:).*?\*/", "", _base_css(m), flags=re.S)   # drop prose comments
    unannotated, exempt, bad_bg = [], set(), []
    for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        selector = " ".join(selector.split())
        for decl in re.finditer(r"(?<![-\w])(?:color|fill)\s*:[^;]*;(\s*/\*\s*on:\s*([^*]*?)\s*\*/)?", body):
            if not decl.group(1):
                unannotated.append(selector)
                continue
            targets = [t.strip() for t in decl.group(2).split(",")]
            if targets == ["-"]:
                exempt.add(selector)
            else:
                bad_bg += [(selector, t) for t in targets if t not in m.THEME_COLOR_SLOTS]
    check("every text colour carries an on: annotation", unannotated, [])
    check("only the named few are exempt", sorted(exempt - EXEMPT), [])
    check("every annotated background is a slot", bad_bg, [])
    # The guard can fail: strip one annotation and it is caught.
    broken = _base_css(m).replace("color: var(--mastheadMeta); /* on: mastheadBg */", "color: var(--mastheadMeta);")
    check("the data-currency rule is annotated", broken != _base_css(m), True)
    check("...and losing it drops the pair", ("mastheadMeta", "mastheadBg") in m.theme_contrast_pairs(broken), False)


def test_contrast_pairs_cover_what_the_first_draft_missed(m):
    """The first draft checked four pairs and missed every one of these."""
    pairs = m.theme_contrast_pairs(_base_css(m))
    for pair in [("mastheadMeta", "mastheadBg"),        # the data-currency line
                 ("mastheadAccentInk", "mastheadBg"),   # the stack name
                 ("ink3", "pageBg"),                    # footer "Verify against the live stack"
                 ("ink", "zebra"), ("ink2", "calloutBg"), ("mastheadSub", "mastheadBg")]:
        check("pair %s on %s is checked" % pair, pair in pairs, True)
    check("accent is never text", [p for p in pairs if p[0] in ("accent", "accentStrong")], [])


def test_shipped_themes_pass(m):
    pairs = m.theme_contrast_pairs(_base_css(m))
    check("the default theme passes", m.theme_problems(dict(m.DEFAULT_THEME), pairs), [])
    check("the default theme names no organisation", m.DEFAULT_THEME["producer"], "meridiancs")
    t = m._active_theme()
    if t["source"] != "bundled":
        pytest.skip("public tree bundles no theme")
    check("the bundled theme is used, not rejected", t.get("fallbackReason"), None)
    check("...and it is Cyderes", (t["name"], t["producer"]), ("cyderes", "Cyderes Meridian"))


def test_value_grammar(m, assets):
    """Colours are the only theme values that enter <style>, and only as #RRGGBB."""
    for bad in ["red} .crit{display:none", "#fff", "red", "rgb(0,0,0)", "var(--ink)", "url(x)",
                "#000000;}", "#00000g", "expression(1)", "#000000\n", "</style>"]:
        t = assets({"themeApi": 1, "accent": bad})
        check("accent %r rejected" % bad, t["source"], "default")
        check("...saying which slot", "accent" in t.get("fallbackReason", ""), True)
    t = assets({"themeApi": 1, "accent": "#F2C14E"})
    check("a valid accent-only theme is used", t["source"], "bundled")
    css = m._load_css()
    check("...with its value in place", "--accent: #F2C14E;" in css, True)
    check("...and on the masthead text, which it inherits", t["mastheadAccentInk"], "#F2C14E")
    t = assets({"themeApi": 1, "accent": "#123456"})
    check("a dark accent fails as masthead text", "mastheadAccentInk on mastheadBg" in t.get("fallbackReason", ""), True)


def test_theme_cannot_hide_the_currency_line(m, assets, tmp_path, capsys, monkeypatch):
    """`mastheadMeta` equal to `mastheadBg` would have made "Data currency could not be confirmed"
    invisible -- the line design/data-currency.md exists to keep on the page."""
    t = assets({"themeApi": 1, "mastheadBg": "#222222", "mastheadMeta": "#222222"})
    check("rejected", t["source"], "default")
    check("...naming the pair", "mastheadMeta on mastheadBg" in t["fallbackReason"], True)
    inp = tmp_path / "in.json"
    inp.write_text(json.dumps({"table": "asset", "totalRecords": 1, "shown": 1, "rows": [{"A": 1}],
                               "dataCurrency": {"class": "unknown", "reason": "stamp unreadable"}}))

    class A:
        title, date, html = None, None, True
    a = A()
    a.input, a.out = str(inp), str(tmp_path / "r.html")
    monkeypatch.setattr(m, "load_config", lambda: ("s.example", "tok", None))
    m.cmd_report(a)
    res = json.loads(capsys.readouterr().out)
    check("the report says it fell back, and why", (res["theme"]["source"], "mastheadMeta" in res["theme"].get("fallbackReason", "")),
          ("default", True))
    page = (tmp_path / "r.html").read_text(encoding="utf-8")
    check("the currency line is on the page", "Data currency could not be confirmed" in page, True)
    check("...in the default theme's readable colour", "--mastheadMeta: %s;" % m.DEFAULT_THEME["mastheadMeta"] in page, True)


def test_light_floor(m, assets):
    """A dark content background hides the fixed severity words even when every text pair passes."""
    dark = {"themeApi": 1, "pageBg": "#202020", "panel": "#202020", "zebra": "#262626", "calloutBg": "#202020",
            "ink": "#FFFFFF", "ink2": "#E0E0E0", "ink3": "#C0C0C0"}
    t = assets(dark)
    check("dark mode rejected", t["source"], "default")
    check("...by the floor", "too dark" in t["fallbackReason"], True)
    check("the floor sits below the Cyderes page", m.relative_luminance("#F9F2ED") > m.THEME_LIGHT_FLOOR, True)


def test_contrast_rejections(m, assets):
    t = assets({"themeApi": 1, "ink3": "#F0F0F0"})
    check("faint footer text rejected", "ink3 on" in t.get("fallbackReason", ""), True)
    t = assets({"themeApi": 1, "zebra": "#FFFFFF", "ink": "#FEFEFE"})
    check("invisible table rows rejected", t["source"], "default")


def test_theme_api(m, assets):
    t = assets({"themeApi": 2, "accent": "#F2C14E"})
    check("a newer themeApi falls back, never partly applied", (t["source"], t["fallbackReason"]),
          ("default", "needs-newer-skill (themeApi 2)"))
    t = assets({"accent": "#F2C14E"})
    check("a missing themeApi is rejected", t["source"], "default")
    t = assets({"themeApi": 1, "accent": "#F2C14E", "someFutureSlot": "#000000"})
    check("an unknown optional slot is ignored, not fatal", t["source"], "bundled")
    check("a missing slot takes the default", t["pageBg"], m.DEFAULT_THEME["pageBg"])


def test_text_and_logo_slots(m, assets):
    t = assets({"themeApi": 1, "producer": "x" * 41})
    check("an over-long producer is rejected", t["source"], "default")
    t = assets({"themeApi": 1, "classification": "TLP\x07AMBER"})
    check("a control character is rejected", t["source"], "default")
    t = assets({"themeApi": 1, "logo": "../../config.json"})
    check("a logo path outside the theme is rejected", t["source"], "default")
    t = assets({"themeApi": 1, "logo": "l.svg"})
    check("a missing logo file is rejected", "missing" in t.get("fallbackReason", ""), True)
    t = assets({"themeApi": 1, "logo": "l.svg"},
               {"l.svg": '<!DOCTYPE svg [<!ENTITY x "y">]><svg xmlns="http://www.w3.org/2000/svg"/>'})
    check("a DOCTYPE/ENTITY is refused", "DOCTYPE" in t.get("fallbackReason", ""), True)
    t = assets({"themeApi": 1, "logo": "l.svg", "producer": "Example Co", "classification": "Internal"},
               {"l.svg": '<svg xmlns="http://www.w3.org/2000/svg"><rect width="1" height="1"/></svg>'})
    check("a good logo is used", m._logo_svg().startswith("<svg"), True)
    check("...the producer reaches the footer", "Example Co skill" in m._producer_note(), True)
    check("...and the verify instruction is not themeable", "Verify against the live stack" in m._producer_note(), True)
    check("the product slot falls back to the word", m._meridian_svg(), "Meridian")


def test_fonts_follow_the_theme(m, assets):
    fonts = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "fonts")
    shutil.copytree(fonts, os.path.join(m._assets_dir(), "fonts"))
    assets({"themeApi": 1, "sans": "system-sans", "mono": "system-mono"})
    check("system fonts embed nothing", m._font_face_css(), "")
    assets({"themeApi": 1, "sans": "Space Grotesk", "mono": "system-mono"})
    check("one bundled family embeds its three faces", m._font_face_css().count("@font-face"), 3)
    t = assets({"themeApi": 1, "sans": "Comic Sans"})
    check("a font outside the list is rejected", t["source"], "default")


def test_inline_text_colours_cannot_be_hidden(m, tmp_path, monkeypatch, capsys):
    """Colours meridian.py writes into a page directly sit outside every theme check, so each must be
    a severity colour or dark enough that no background passing the light floor can hide it. The
    trend chart's coverage notes were inline #8a847f until phase 1, which a light theme could have
    hidden at 3:1; they now read the ink3 slot."""
    severity = {"#C0392B", "#AD5212", "#876A0A", "#237A4B"}
    floor_bg = m.THEME_LIGHT_FLOOR

    def safe(hexcolor):
        if hexcolor.upper() in severity:
            return True
        lum = m.relative_luminance(hexcolor)
        return (floor_bg + 0.05) / (lum + 0.05) >= 4.5

    user = {"type": "user", "identity": {"ownerName": "U1", "displayName": "Ada"},
            "risk": {"score": 54.0, "level": "3-high", "ranking": 98.0, "factors": ["F"]},
            "posture": {"mfa": [{"source": "okta_user", "configured": "no"}], "nonCompliance": []},
            "threats": {"leakedCredentialCount": 2, "dlpBehavioral": []},
            "stability": {"oscillating": True, "fields": [{"field": "Owner_Name", "changeCount": 3}]},
            "linkedAssets": [{"asset": "H1", "level": "2-medium", "os": "x", "ip": ["10.0.0.1"], "kev": 1,
                              "encrypted": 0.0, "otherHighRiskUsers": []}]}
    pages = [m._profile_html(user)[2], m._line_chart_svg(["2026-10-01", "2026-10-02", "2026-10-03"],
                                                          [("a", [1, None, 3], "#000000")], flagged=["2026-10-02"]),
             m._chart_block("t", ["2026-10-01", "2026-10-02"], [("a", [1, None])],
                            coverage_flags=[{"date": "2026-10-02", "reason": "x failing"}]),
             m._simple_table(["a"], [])]
    found = []
    for page in pages:
        found += re.findall(r"(?<![-\w])color\s*:\s*(#[0-9A-Fa-f]{6})", page)
        found += re.findall(r"<text[^>]*\sfill=\"(#[0-9A-Fa-f]{3,6})\"", page)
    found = ["#" + "".join(c * 2 for c in h[1:]) if len(h) == 4 else h for h in found]
    check("the guard sees inline colours at all", bool(found), True)
    check("every inline text colour is severity or floor-safe", [h for h in found if not safe(h)], [])
    check("the guard can fail: the old note colour", safe("#8a847f"), False)


def test_public_derivation_drops_the_bundled_theme(monkeypatch):
    import importlib.util
    import sys
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "scripts", "tools", "make-public.py")
    if not os.path.exists(path):
        pytest.skip("public tree has no derivation script")
    monkeypatch.setattr(sys, "path", [os.path.dirname(path)] + sys.path)
    spec = importlib.util.spec_from_file_location("make_public", path)
    mp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mp)
    keep, _subs, dropped, _missing = mp.plan(["assets/report-base.css", "assets/themes/cyderes/theme.json",
                                              "assets/themes/cyderes/NOTICE", "assets/themes/cyderes/cyderes-logo.svg"])
    check("the layout ships publicly", "assets/report-base.css" in keep, True)
    check("no file of the bundled theme does", [f for f in keep if f.startswith("assets/themes/")], [])
