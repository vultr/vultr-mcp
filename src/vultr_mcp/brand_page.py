"""Vultr's sign-in look for the pages this server renders, matching the console's OAuth consent screen.

Mirrors my.vultr.com's AuthLayout (features/iam/organizations/oauth/AuthorizeOAuthApplication): its navy
panel, nav logo, concentric waves, heading, footer and "Contact us" link, at the same sizes and colours, so
moving between the console and these pages reads as one flow. The logo and waves are the console's own public
assets (vultr_nav_logo.svg, concentric-waves-wide.svg), embedded as data URIs so nothing loads from elsewhere.
"""

from __future__ import annotations

import base64
import html
import os
from datetime import datetime, timezone
from urllib.parse import urlparse

from vultr_mcp.branding import STATIC_DIR


def _data_uri(name: str) -> str:
    """The asset as a data URI, or "" when it is missing so the page still renders without it."""
    try:
        encoded = base64.b64encode((STATIC_DIR / name).read_bytes()).decode("ascii")
    except OSError:
        return ""
    return f"data:image/svg+xml;base64,{encoded}"


LOGO = _data_uri("vultr-nav-logo.svg")
WAVES = _data_uri("vultr-waves-wide.svg")

# The console footer's links (AuthLayout Footer constants), on www.vultr.com.
_FOOTER_LINKS = (
    ("Cookie Policy", "https://www.vultr.com/legal/cookie-policy/"),
    ("Privacy Policy", "https://www.vultr.com/legal/privacy/"),
    ("Terms of Service", "https://www.vultr.com/legal/tos/"),
    ("FAQ", "https://www.vultr.com/resources/faq/"),
)
_SUPPORT_URL = "https://my.vultr.com/support/"

# lucide's Headset icon, which the console's "Contact us" button carries.
_HEADSET = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" '
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<path d="M3 11h3a2 2 0 0 1 2 2v3a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-5Zm0 0a9 9 0 1 1 18 0m0 0v5a2 2 0 0 1-2 '
    '2h-1a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2h3Z"/><path d="M21 16v2a4 4 0 0 1-4 4h-5"/></svg>'
)

# The console's tokens (my.vultr.com _css/tailwind.css) and AuthLayout's Tailwind classes, as plain CSS.
CSS = """
:root{--vx-navy:hsl(230.93 93.48% 18.04%);--vx-navy-100:hsl(232.94 20.48% 83.73%);
  --vx-accent:hsl(204.14 100% 65.88%);--vx-blue:hsl(210.71 100% 49.41%);--vx-blue-600:hsl(210.89 100% 39.61%);
  --vx-fg:hsl(240 6% 10%);--vx-muted:hsl(240 4% 46%);--vx-border:hsl(240 6% 90%);--vx-field:hsl(240 5% 96%);
  --vx-hover:hsl(214 100% 97%)}
*{box-sizing:border-box}
html,body{margin:0;height:100%}
body{display:flex;min-height:100%;align-items:stretch;justify-content:flex-start;padding:0;background:#fff;
  color:var(--vx-fg);font:16px/1.5 Roboto,ui-sans-serif,system-ui,-apple-system,"Segoe UI",Helvetica,Arial,sans-serif}
.vx-panel{position:relative;display:none;width:33.333%;max-width:640px;flex-shrink:0;
  background:var(--vx-navy);padding:48px 64px;overflow:hidden}
.vx-waves{pointer-events:none;position:absolute;inset:0;width:640px;
  background:WAVES_BG no-repeat right top/cover}
.vx-panel-inner{position:relative;display:flex;height:100%;flex-direction:column;justify-content:space-between}
.vx-logo{width:184px;object-fit:contain}
.vx-headline{margin:0;font-size:2.25rem;line-height:2.5rem;font-weight:700;color:#fff}
.vx-headline span{color:var(--vx-accent)}
.vx-foot{display:flex;flex-direction:column;row-gap:8px;color:var(--vx-navy-100)}
.vx-foot-links{display:flex;flex-wrap:wrap;column-gap:4px;font-size:.875rem;line-height:1.25rem}
.vx-foot-links a{color:#fff;text-decoration:underline;text-underline-offset:4px}
.vx-foot p{margin:0;font-size:.75rem;line-height:1rem}
.vx-main{position:relative;display:flex;width:100%;flex-direction:column;align-items:center;justify-content:center;
  background:#fff;padding:16px}
.vx-contact{position:absolute;right:24px;top:24px;display:inline-flex;align-items:center;gap:8px;height:36px;
  padding:8px 16px;border-radius:6px;color:var(--vx-fg);font-size:.875rem;font-weight:500;text-decoration:none}
.vx-contact:hover{background:var(--vx-hover)}
.vx-content{display:flex;width:100%;max-width:400px;flex-direction:column;row-gap:24px}
@media (min-width:640px){.vx-main{padding:24px}}
@media (min-width:1024px){.vx-panel{display:block}.vx-main{padding:48px 64px}.vx-contact{right:64px;top:48px}}
"""


def _css() -> str:
    background = f'url("{WAVES}")' if WAVES else "none"
    return CSS.replace("WAVES_BG", background)


def _host() -> str:
    return urlparse(os.environ.get("MCP_RESOURCE_URL", "https://mcp.vultr.com")).hostname or "mcp.vultr.com"


def panel(accent: str, rest: str) -> str:
    """The console's left panel: logo at the top, heading with its accent in light blue, footer at the bottom."""
    logo = f'<img class="vx-logo" src="{LOGO}" alt="Vultr logo">' if LOGO else "<span></span>"
    links = " <span aria-hidden=\"true\">•</span> ".join(
        f'<a href="{href}">{label}</a>' for label, href in _FOOTER_LINKS
    )
    year = datetime.now(timezone.utc).year
    return (
        '<aside class="vx-panel"><div class="vx-waves" aria-hidden="true"></div><div class="vx-panel-inner">'
        f'{logo}<h1 class="vx-headline"><span>{html.escape(accent)}</span> {html.escape(rest)}</h1>'
        f'<footer class="vx-foot"><div class="vx-foot-links">{html.escape(_host())} - {links}</div>'
        f"<p>© Vultr {year} | VULTR is a registered trademark of The Constant Company, LLC.</p></footer>"
        "</div></aside>"
    )


def contact_link() -> str:
    return (
        f'<a class="vx-contact" href="{_SUPPORT_URL}" target="_blank" rel="noreferrer">Contact us{_HEADSET}'
        '<span style="position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0,0,0,0)">'
        "(opens in new tab)</span></a>"
    )


def page(title: str, accent: str, rest: str, main: str, extra_css: str = "", script: str = "") -> str:
    """A whole document: the panel, then `main` in the console's 400px content column."""
    return (
        "<!doctype html><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f'<title>{html.escape(title)}</title><link rel="icon" href="/favicon.ico">'
        f"<style>{_css()}{extra_css}</style>"
        f'{panel(accent, rest)}<main class="vx-main">{contact_link()}<div class="vx-content">{main}</div></main>{script}'
    )


# FastMCP's consent page in the console's colours; it comes after FastMCP's own styles, so equal selectors win.
CONSENT_CSS = """
.vx-main .container{width:100%;min-width:0;max-width:none;margin:0;padding:0;background:none;border:0;
  box-shadow:none;text-align:left}
.vx-main .container .logo{display:none}
.vx-main h1{margin:0 0 20px;font-size:1.5rem;line-height:2rem;font-weight:400;color:var(--vx-fg);text-align:left}
.vx-main .info-box{background:var(--vx-hover);border:1px solid var(--vx-border);border-radius:8px;color:var(--vx-fg)}
.vx-main .info-box strong,.vx-main .server-name-link{color:var(--vx-fg)}
.vx-main .redirect-section{background:var(--vx-field);border:1px solid var(--vx-border);border-radius:8px}
.vx-main .redirect-section .label{color:var(--vx-muted)}
.vx-main .redirect-section .value,.vx-main .detail-value{color:var(--vx-fg);overflow-wrap:anywhere}
.vx-main details summary{color:var(--vx-fg)}
.vx-main .detail-box{background:var(--vx-field);border-color:var(--vx-border)}
.vx-main .button-group{display:flex;flex-direction:column;gap:12px;margin-top:24px}
.vx-main .button-group button{width:100%;margin:0;height:40px;padding:8px 16px;border-radius:6px;font-size:.875rem;
  font-weight:500;cursor:pointer;transition:background .15s,color .15s}
.vx-main .btn-approve{background:var(--vx-blue);color:#fff;border:0}
.vx-main .btn-approve:hover{background:var(--vx-blue-600)}
.vx-main .btn-deny{background:var(--vx-field);color:var(--vx-fg);border:0}
.vx-main .btn-deny:hover{background:var(--vx-border)}
.help-link,.tooltip-link{color:var(--vx-muted)}
"""


def restyle_consent(document: str) -> str:
    """Put FastMCP's consent page inside the console layout; anything unexpected is returned unchanged."""
    if "</head>" not in document or "<body>" not in document or "</body>" not in document:
        return document
    styled = document.replace("</head>", f"<style>{_css()}{CONSENT_CSS}</style></head>", 1)
    styled = styled.replace(
        "<body>",
        f'<body>{panel("Connect", "your app to Vultr.")}<main class="vx-main">{contact_link()}<div class="vx-content">',
        1,
    )
    # The help link sits outside FastMCP's container, so the content column and main area close before it.
    marker = '<div class="help-link-container">'
    if marker in styled:
        styled = styled.replace(marker, f"</div></main>{marker}", 1)
    else:
        styled = styled.replace("</body>", "</div></main></body>", 1)
    return styled.replace("This FastMCP server requires", "The Vultr MCP Server requires", 1)
