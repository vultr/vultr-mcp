"""Server-side rescue for loopback callbacks the browser cannot reach.

A CLI listening on 127.0.0.1 only gets the code when the browser is on the same machine. For
loopback targets the 302 becomes a page that tries to deliver the code with ``fetch``, and always
also offers a link and both the callback URL and the bare code to paste, since browsers may block
the probe and its success proves nothing. Non-loopback redirects get the ordinary 302.
"""

from __future__ import annotations

import html
import ipaddress
import json
from urllib.parse import parse_qs, urlparse

from starlette.responses import HTMLResponse

from vultr_mcp import brand_page

# Hostnames that mean "this machine" without needing to be parsed as an IP.
_LOOPBACK_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost"}


def is_loopback_target(url: str) -> bool:
    """True when a redirect target can only be served by the viewing machine (127.0.0.0/8, ::1, localhost)."""
    try:
        host = (urlparse(url).hostname or "").strip().lower()
    except ValueError:
        return False
    if not host:
        return False
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


# Schemes a browser must never be sent to from a page we render, whatever a client registered.
_UNSAFE_SCHEMES = {"javascript", "data", "vbscript", "file", "blob", "about"}


def is_app_link(url: str) -> bool:
    """True for a desktop app's own scheme (claude://...), where a 302 leaves the tab on the page before it."""
    try:
        scheme = (urlparse(url).scheme or "").lower()
    except ValueError:
        return False
    return bool(scheme) and scheme not in ("http", "https") and scheme not in _UNSAFE_SCHEMES


def _param(url: str, name: str) -> str:
    try:
        return (parse_qs(urlparse(url).query).get(name) or [""])[0]
    except ValueError:
        return ""


# The content beside brand_page's navy panel; the palette variables come from brand_page.
_CSS = """
.card{width:100%;min-width:0}
h1{margin:0 0 12px;font-size:1.5rem;line-height:2rem;font-weight:400;color:var(--vx-fg)}
p{margin:0 0 20px;color:var(--vx-muted)}
a{color:var(--vx-blue)}
.code{display:flex;gap:8px;align-items:stretch;margin:0 0 16px}
.code input{flex:1;min-width:0;padding:11px 13px;font-size:13px;
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;border:1px solid var(--vx-border);
  border-radius:6px;background:var(--vx-field);color:var(--vx-fg)}
.code button,.btn{border:0;border-radius:6px;background:var(--vx-blue);color:#fff;
  font-weight:500;font-size:.875rem;cursor:pointer;transition:background .15s}
.code button{padding:8px 16px;white-space:nowrap}
.btn{display:flex;align-items:center;justify-content:center;width:100%;height:40px;padding:8px 16px;text-decoration:none;margin:0 0 18px}
.code button:hover,.btn:hover{background:var(--vx-blue-600)}
.ok{width:52px;height:52px;border-radius:50%;background:rgba(0,123,252,.1);color:var(--vx-blue);
  display:flex;align-items:center;justify-content:center;font-size:26px;font-weight:700;margin-bottom:18px}
.spin{width:24px;height:24px;border:3px solid var(--vx-border);border-top-color:var(--vx-blue);
  border-radius:50%;animation:s .8s linear infinite;margin-bottom:16px}
@keyframes s{to{transform:rotate(360deg)}}
[hidden]{display:none!important}
small{color:var(--vx-muted);font-size:13px}
details{margin-top:20px;border-top:1px solid var(--vx-border);padding-top:16px}
details summary{cursor:pointer;font-weight:600;font-size:14px;color:var(--vx-fg);margin-bottom:12px}
details[open] summary{margin-bottom:14px}
"""

# The page carries an authorization code: it must not be cached by the browser or anything between us and it.
_NO_STORE = {
    "Cache-Control": "no-store, no-cache, must-revalidate",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
}


def app_return_page(target: str) -> HTMLResponse:
    """Open the app and tell the person they are done, since the browser keeps no page of its own for an app link."""
    safe_target = html.escape(target, quote=True)
    # Entities are not decoded inside <script>, so the script gets JSON with "<" escaped instead.
    script_target = json.dumps(target).replace("<", "\\u003c")
    card = f"""<div class="card">
  <div class="ok">&#10003;</div>
  <h1>You're signed in to Vultr</h1>
  <p>Your app is connected to the Vultr MCP Server. You can close this tab once the app opens.</p>
  <a class="btn" href="{safe_target}">Open the app</a>
  <small>Nothing happened? Use the button above; your browser may ask before it opens the app.</small>
</div>"""
    return HTMLResponse(
        brand_page.page(
            "Signed in to Vultr", "Signed in", "to Vultr.", card, _CSS,
            f"<script>window.location.replace({script_target});</script>",
        ),
        headers=_NO_STORE,
    )


def completion_page(target: str) -> HTMLResponse:
    """Render the hand-off page for an unreachable-or-not loopback callback.

    The probe runs in the browser because that is the only vantage point that
    can answer the question at all: the server cannot see whether the viewer's
    machine has something listening on its own loopback port.
    """
    code = _param(target, "code")
    safe_target = html.escape(target, quote=True)
    safe_code = html.escape(code, quote=True)

    card = f"""<div class="card">
  <div id="probe">
    <div class="spin"></div>
    <h1>Finishing up</h1>
    <p>Handing the authorization back to your client&hellip;</p>
  </div>

  <div id="done" hidden>
    <div class="ok">&#10003;</div>
    <h1>Handed back</h1>
    <p>Your client should have picked this up. Check your terminal.</p>
  </div>

  <div id="manual" hidden>
    <h1>Almost there</h1>
    <p>The browser could not hand the authorization back on its own.</p>
  </div>

  <details id="ways" hidden open>
    <summary>Terminal still waiting?</summary>
    <p>If the client that started the sign-in is running on <b>this
       machine</b>, finish it here:</p>
    <p><a class="btn" href="{safe_target}" target="_blank"
          rel="noopener noreferrer">Finish sign-in</a></p>
    <p>Or hand it over by hand. Most clients want the whole callback URL
       &mdash; it is what your address bar would have shown:</p>
    <div class="code">
      <input id="u" value="{safe_target}" readonly onclick="this.select()">
      <button type="button" onclick="cp('u')">Copy</button>
    </div>
    <p>Some want the code on its own instead:</p>
    <div class="code">
      <input id="c" value="{safe_code}" readonly onclick="this.select()">
      <button type="button" onclick="cp('c')">Copy</button>
    </div>
    <p><small>Command-line clients take one or the other &mdash; a
       <code>URL &gt;</code> prompt wants the first, a <code>--code</code>
       option the second. Both are single use and expire within a few
       minutes.</small></p>
  </details>
</div>"""
    script = f"""<script>
// Deliver the code to a listener on THIS machine, if there is one. When it
// works this is the request the client has been waiting for and it completes
// exactly as it would have after a redirect. no-cors because we never need to
// read the reply -- only to make the request arrive.
//
// A rejection is NOT a verdict that the client is remote: a browser that
// gates public-to-local requests refuses this one without asking the network.
// So the fallback panel carries both ways out, and does not claim to know
// which applies.
var target = "{safe_target}";
function show(id) {{
  document.getElementById("probe").hidden = true;
  document.getElementById(id).hidden = false;
  // Always reachable AND always open: a probe that resolved cannot prove the
  // client accepted anything, so there is no outcome that earns folding these
  // away. Observed with Claude Code, whose probe resolves every time and
  // delivers nothing -- behind a summary, the way out is something you find
  // only after deciding the page lied to you.
  document.getElementById("ways").hidden = false;
}}
function cp(which) {{
  var el = document.getElementById(which);
  el.select();
  (navigator.clipboard ? navigator.clipboard.writeText(el.value)
                       : Promise.reject()).catch(function () {{
    try {{ document.execCommand("copy"); }} catch (e) {{}}
  }});
}}
var settled = false;
function settle(id) {{ if (!settled) {{ settled = true; show(id); }} }}
// A listener that is present answers fast; one that is absent refuses fast.
// The timer only covers the case where the connection hangs instead.
setTimeout(function () {{ settle("manual"); }}, 4000);
fetch(target, {{ mode: "no-cors", cache: "no-store" }})
  .then(function () {{ settle("done"); }})
  .catch(function () {{ settle("manual"); }});
</script>"""
    return HTMLResponse(
        brand_page.page("Finish connecting", "Finish", "connecting your client.", card, _CSS, script),
        headers=_NO_STORE,
    )
