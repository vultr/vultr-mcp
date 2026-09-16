"""Server-side rescue for loopback callbacks that cannot be reached.

A CLI listening on ``http://127.0.0.1:<port>/callback`` only receives the code
when the browser is on the same machine. An agent on an instance reached over
SSH is not, so a successful authorization ends on ``ERR_CONNECTION_REFUSED``
with a valid code stranded in the URL bar.

We issue that code, so for loopback targets the 302 is replaced with a page
that delivers it to the listener itself via ``fetch`` -- keeping the
same-machine case automatic -- and shows it for pasting if that fails. Removing
the human step entirely needs the client to poll, which is ``device_flow``.

Non-loopback redirects get the ordinary 302.
"""

from __future__ import annotations

import html
import ipaddress
from urllib.parse import parse_qs, urlparse

from starlette.responses import HTMLResponse

# Hostnames that mean "this machine" without needing to be parsed as an IP.
_LOOPBACK_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost"}


def is_loopback_target(url: str) -> bool:
    """True when a redirect target can only be served by the viewing machine.

    Covers ``127.0.0.0/8`` (not just ``127.0.0.1``), IPv6 ``::1`` including its
    bracketed form, and the ``localhost`` names. Anything else -- a public
    host, a LAN address, a gateway origin -- is reachable in principle and is
    left alone.
    """
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


def _param(url: str, name: str) -> str:
    try:
        return (parse_qs(urlparse(url).query).get(name) or [""])[0]
    except ValueError:
        return ""


_CSS = """
:root {
  color-scheme: light dark;
  --bg:#fff; --fg:#10151c; --muted:#5a6572; --card:#f6f8fa;
  --line:#d7dde5; --accent:#0057d9; --ok:#137a43;
}
@media (prefers-color-scheme: dark) {
  :root { --bg:#0f1419; --fg:#e8edf3; --muted:#97a3b2; --card:#171d25;
          --line:#2a333e; --accent:#5b9dff; --ok:#3ecf8e; }
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
  font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  display:flex;align-items:center;justify-content:center;min-height:100vh;padding:24px}
.card{width:100%;max-width:520px;background:var(--card);border:1px solid var(--line);
  border-radius:12px;padding:28px}
h1{margin:0 0 6px;font-size:19px}
p{margin:0 0 16px;color:var(--muted)}
.code{display:flex;gap:8px;align-items:stretch;margin:0 0 16px}
.code input{flex:1;padding:12px 14px;font-size:14px;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace;border:1px solid var(--line);
  border-radius:8px;background:var(--bg);color:var(--fg)}
.code button{padding:12px 16px;font-size:14px;font-weight:600;border:0;border-radius:8px;
  background:var(--accent);color:#fff;cursor:pointer;white-space:nowrap}
.ok{font-size:40px;line-height:1;margin-bottom:10px;color:var(--ok)}
.spin{width:20px;height:20px;border:2px solid var(--line);border-top-color:var(--accent);
  border-radius:50%;animation:s .8s linear infinite;margin-bottom:14px}
@keyframes s{to{transform:rotate(360deg)}}
[hidden]{display:none!important}
small{color:var(--muted);font-size:13px}
"""


def completion_page(target: str) -> HTMLResponse:
    """Render the hand-off page for an unreachable-or-not loopback callback.

    The probe runs in the browser because that is the only vantage point that
    can answer the question at all: the server cannot see whether the viewer's
    machine has something listening on its own loopback port.
    """
    code = _param(target, "code")
    safe_target = html.escape(target, quote=True)
    safe_code = html.escape(code, quote=True)

    return HTMLResponse(
        f"""<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Finish connecting</title><style>{_CSS}</style>
<div class="card">
  <div id="probe">
    <div class="spin"></div>
    <h1>Finishing up</h1>
    <p>Handing the authorization back to your client&hellip;</p>
  </div>

  <div id="done" hidden>
    <div class="ok">&#10003;</div>
    <h1>Connected</h1>
    <p>You can close this window and return to your terminal.</p>
  </div>

  <div id="manual" hidden>
    <h1>Almost there</h1>
    <p>Your client is running on a different machine than this browser, so it
       could not receive the authorization automatically. Copy this code and
       paste it into the client that started the sign-in.</p>
    <div class="code">
      <input id="c" value="{safe_code}" readonly onclick="this.select()">
      <button type="button" onclick="cp()">Copy</button>
    </div>
    <p><small>Most command-line clients accept it with a
       <code>--code</code> option. The code is single use and expires within a
       few minutes.</small></p>
  </div>
</div>
<script>
// Deliver the code to a listener on THIS machine, if there is one. When the
// client is local this is the request it has been waiting for and it
// completes exactly as it would have after a redirect. When the client is on
// a remote host the request cannot connect and we fall through to the paste
// path. no-cors because we never need to read the reply -- only to make the
// request arrive.
var target = "{safe_target}";
function show(id) {{
  document.getElementById("probe").hidden = true;
  document.getElementById(id).hidden = false;
}}
function cp() {{
  var el = document.getElementById("c");
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
</script>""",
        headers={
            # The page carries an authorization code: it must not be cached by
            # the browser or anything between us and it.
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer",
        },
    )
