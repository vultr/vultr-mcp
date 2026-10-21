"""Vultr's sign-in look for the pages this server renders: the console's navy panel beside a white content area.

The logo and the line pattern are Vultr's own public console assets (my.vultr.com's logo__on-dark.svg and
waves-left.svg), embedded as data URIs so the pages load nothing from elsewhere.
"""

from __future__ import annotations

import base64
import html

from vultr_mcp.branding import STATIC_DIR


def _data_uri(name: str) -> str:
    """The asset as a data URI, or "" when it is missing so the page still renders without it."""
    try:
        encoded = base64.b64encode((STATIC_DIR / name).read_bytes()).decode("ascii")
    except OSError:
        return ""
    return f"data:image/svg+xml;base64,{encoded}"


LOGO = _data_uri("vultr-logo-on-dark.svg")
WAVES = _data_uri("vultr-waves.svg")

# The console's palette: navy panel, light-blue headline accent, Vultr blue actions, its greys for text.
CSS = """
:root{--navy:#031059;--sky:#51b9ff;--blue:#007bfc;--blue-hover:#375ac8;
  --fg:#18181b;--muted:#71717a;--line:#e4e4e7;--field:#f4f4f5}
*{box-sizing:border-box}
html,body{margin:0;min-height:100vh}
body{display:flex;align-items:stretch;justify-content:flex-start;padding:0;background:#fff;color:var(--fg);
  font:15px/1.55 Roboto,-apple-system,"system-ui",BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}
.vx-panel{flex:0 0 33%;min-width:380px;max-width:640px;display:flex;flex-direction:column;
  justify-content:space-between;padding:64px 72px 96px;color:#fff;
  background:var(--navy) WAVES_BG no-repeat left bottom/100% auto}
.vx-logo{width:255px;max-width:100%;height:auto}
.vx-headline{margin:0;font-size:46px;line-height:1.12;font-weight:700;letter-spacing:-.01em}
.vx-headline em{font-style:normal;color:var(--sky)}
.vx-main{flex:1;display:flex;align-items:center;justify-content:center;padding:48px 32px;min-width:0}
@media (max-width:900px){
  body{flex-direction:column}
  .vx-panel{flex:none;min-width:0;max-width:none;padding:28px 24px;gap:28px;background-size:100% auto;background-position:left top}
  .vx-logo{width:150px}
  .vx-headline{font-size:28px}
  .vx-main{align-items:flex-start;padding:32px 20px}
}
"""


def _css() -> str:
    background = f'url("{WAVES}")' if WAVES else "none"
    return CSS.replace("WAVES_BG", background)


def panel(accent: str, rest: str) -> str:
    """The navy panel: logo above, the headline with its first words in light blue below."""
    logo = f'<img class="vx-logo" src="{LOGO}" alt="Vultr">' if LOGO else ""
    return (
        f'<aside class="vx-panel">{logo}'
        f'<h2 class="vx-headline"><em>{html.escape(accent)}</em> {html.escape(rest)}</h2></aside>'
    )


def page(title: str, accent: str, rest: str, main: str, extra_css: str = "", script: str = "") -> str:
    """A whole document: the panel, then `main` in the white area."""
    return (
        "<!doctype html><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f'<title>{html.escape(title)}</title><link rel="icon" href="/favicon.ico">'
        f"<style>{_css()}{extra_css}</style>"
        f'{panel(accent, rest)}<main class="vx-main">{main}</main>{script}'
    )


# FastMCP's consent page in Vultr's colours; it comes after FastMCP's own styles, so equal selectors win.
CONSENT_CSS = """
.vx-main .container{width:100%;min-width:0;max-width:520px;margin:0;padding:0;background:none;border:0;
  box-shadow:none;text-align:left}
.vx-main .container .logo{display:none}
.vx-main h1{margin:0 0 20px;font-size:28px;font-weight:400;color:var(--fg);text-align:left}
.vx-main .info-box{background:rgba(0,123,252,.05);border:1px solid #d4e0fb;border-radius:8px;color:var(--fg)}
.vx-main .info-box strong,.vx-main .server-name-link{color:var(--navy)}
.vx-main .redirect-section{background:var(--field);border:1px solid var(--line);border-radius:8px}
.vx-main .redirect-section .label{color:var(--muted)}
.vx-main .redirect-section .value,.vx-main .detail-value{color:var(--fg);overflow-wrap:anywhere}
.vx-main details summary{color:var(--navy)}
.vx-main .detail-box{background:var(--field);border-color:var(--line)}
.vx-main .button-group{display:flex;flex-direction:column;gap:12px;margin-top:28px}
.vx-main .button-group button{width:100%;margin:0;padding:14px 18px;border-radius:6px;font-size:16px;
  font-weight:600;cursor:pointer;transition:background .15s,color .15s}
.vx-main .btn-approve{background:var(--blue);color:#fff;border:0}
.vx-main .btn-approve:hover{background:var(--blue-hover)}
.vx-main .btn-deny{background:var(--field);color:var(--muted);border:0}
.vx-main .btn-deny:hover{background:var(--line);color:var(--fg)}
.help-link,.tooltip-link{color:var(--muted)}
"""


def restyle_consent(document: str) -> str:
    """Put FastMCP's consent page inside the Vultr layout; anything unexpected is returned unchanged."""
    if "</head>" not in document or "<body>" not in document or "</body>" not in document:
        return document
    styled = document.replace("</head>", f"<style>{_css()}{CONSENT_CSS}</style></head>", 1)
    styled = styled.replace(
        "<body>", f'<body>{panel("Connect", "your app to Vultr.")}<main class="vx-main">', 1
    )
    # The help link sits outside FastMCP's container, so the main area closes before it.
    marker = '<div class="help-link-container">'
    if marker in styled:
        styled = styled.replace(marker, f"</main>{marker}", 1)
    else:
        styled = styled.replace("</body>", "</main></body>", 1)
    return styled.replace("This FastMCP server requires", "The Vultr MCP Server requires", 1)
