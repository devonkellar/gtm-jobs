#!/usr/bin/env python3
"""
site_shell.py — the chrome every ops page shares: nav, base CSS, theme handling.

One place, so a second page cannot quietly diverge from the first. Each page
supplies its own body, its own extra CSS and its own JS; this supplies the frame.

THE NAV IS THE POINT
--------------------
Before this, the reporting page and the domain checker were separate artefacts
with no way to get from one to the other. The ops site is meant to answer both
"who replied" and "can we still send", so the two have to be one click apart or
nobody will look at the second one.

Adding a page: add it to PAGES, write a build_*.py that calls page(), and give
deploy_site.py its filename. Nothing else.
"""

# (slug, filename, label). Order is the nav order.
PAGES = [
    ("replies", "index.html", "Replies"),
    ("deliverability", "deliverability.html", "Domain health"),
]

BASE_CSS = """
:root{--paper:#F6EFE3;--card:#FFFBF4;--ink:#2B2118;--mut:#6B5B4A;--rule:#DCCEB8;
--terra:#B4552D;--ok:#3F6B3F;--okb:#DFEBDD;--hot:#9B3226;--hotb:#F5DAD5;
--inf:#8A6A1F;--infb:#F4E9CC;--neg:#6B5B4A;--negb:#EDE4D6}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]){
--paper:#191512;--card:#211C17;--ink:#EFE6D8;--mut:#AD9C87;--rule:#3A3129;
--terra:#E08A5C;--ok:#8FBF8A;--okb:#1E2C1E;--hot:#E28878;--hotb:#331C18;
--inf:#D9B75E;--infb:#2E2716;--neg:#AD9C87;--negb:#26211B}}
:root[data-theme=dark]{--paper:#191512;--card:#211C17;--ink:#EFE6D8;--mut:#AD9C87;
--rule:#3A3129;--terra:#E08A5C;--ok:#8FBF8A;--okb:#1E2C1E;--hot:#E28878;
--hotb:#331C18;--inf:#D9B75E;--infb:#2E2716;--neg:#AD9C87;--negb:#26211B}
*{box-sizing:border-box}
body{background:var(--paper);color:var(--ink);margin:0;
font:15px/1.6 Karla,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:30px 20px 80px}
h1{font-size:2.1rem;margin:0 0 3px;letter-spacing:-.02em}
h2{font-size:1.15rem;margin:34px 0 12px;letter-spacing:-.01em}
.sub{color:var(--mut);margin:0 0 24px;font-size:.92rem}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:13px}
.stat{background:var(--card);border:1px solid var(--rule);box-shadow:3px 3px 0 var(--rule);
padding:14px 16px;text-decoration:none;color:inherit;display:block;cursor:pointer}
.stat:hover{box-shadow:5px 5px 0 var(--terra);transform:translate(-1px,-1px)}
.stat b{font-size:1.9rem;color:var(--terra);display:block;line-height:1.05}
.stat span{font-size:.71rem;text-transform:uppercase;letter-spacing:.07em;color:var(--mut)}
.stat em{font-style:normal;font-size:.72rem;color:var(--mut);display:block;margin-top:3px}
.bar{background:var(--card);border-left:4px solid var(--terra);padding:12px 16px;
box-shadow:3px 3px 0 var(--rule);margin:20px 0}
table{width:100%;border-collapse:collapse;background:var(--card);
border:1px solid var(--rule);box-shadow:3px 3px 0 var(--rule)}
th,td{text-align:left;padding:9px 12px;border-bottom:1px solid var(--rule);
font-size:.88rem;vertical-align:top}
th{font-size:.7rem;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);
cursor:pointer;user-select:none;white-space:nowrap}
th:hover{color:var(--terra)}
tr:last-child td{border-bottom:none}
tbody tr{cursor:pointer}
tbody tr:hover{background:var(--paper)}
.pill{font-size:.68rem;font-weight:700;padding:2px 7px;border-radius:3px;white-space:nowrap}
.p-ok{background:var(--okb);color:var(--ok)}.p-hot{background:var(--hotb);color:var(--hot)}
.p-inf{background:var(--infb);color:var(--inf)}.p-neg{background:var(--negb);color:var(--neg)}
.scroll{overflow-x:auto}
.tools{display:flex;gap:9px;flex-wrap:wrap;margin:16px 0 10px;align-items:center}
input[type=search],select{background:var(--card);color:var(--ink);border:1px solid var(--rule);
padding:7px 10px;font:inherit;font-size:.86rem}
input[type=search]{flex:1;min-width:190px}
.count{color:var(--mut);font-size:.82rem}
.body{white-space:pre-wrap;font-size:.85rem;background:var(--paper);
border:1px solid var(--rule);padding:11px 13px;margin-top:8px;max-height:300px;overflow:auto}
.hide{display:none}
.nm{font-weight:700}.em{color:var(--mut);font-size:.8rem}
footer{margin-top:44px;padding-top:14px;border-top:1px solid var(--rule);
color:var(--mut);font-size:.8rem}
"""

NAV_CSS = """
.top{border-bottom:1px solid var(--rule);margin-bottom:26px}
.topin{max-width:1080px;margin:0 auto;padding:0 20px;display:flex;gap:22px;
align-items:baseline;flex-wrap:wrap}
.brand{font-weight:700;letter-spacing:-.01em;padding:14px 0;margin-right:4px}
.top a{color:var(--mut);text-decoration:none;font-size:.88rem;padding:14px 0;
border-bottom:2px solid transparent;margin-bottom:-1px}
.top a:hover{color:var(--ink)}
.top a.on{color:var(--terra);border-bottom-color:var(--terra);font-weight:700}
"""


def nav(active):
    # Clean URLs, no ".html". Cloudflare Pages answers /deliverability.html with a
    # 308 to /deliverability, so linking the filename would send every click
    # through a redirect -- and curl -sS reads that 308 as an empty page, which is
    # exactly how this looked broken when it was not.
    links = "".join(
        f'<a href="/{fn.removesuffix(".html").replace("index", "")}"'
        f'{" class=\"on\"" if slug == active else ""}>{label}</a>'
        for slug, fn, label in PAGES)
    return f'<div class="top"><div class="topin">' \
           f'<span class="brand">Devon Kellar &middot; Ops</span>{links}</div></div>'


def page(title, active, body, css="", js="", footer=""):
    """The full HTML document. `title` is the page name, not the site name."""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow,noarchive">
<title>{title} &middot; Ops</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Karla:wght@300..800&display=swap">
<style>{BASE_CSS}{NAV_CSS}{css}</style>
</head>
<body>
{nav(active)}
<div class="wrap">
<h1>{title}</h1>
{body}
<footer>{footer}</footer>
</div>
<script>{js}</script>
</body>
</html>"""
