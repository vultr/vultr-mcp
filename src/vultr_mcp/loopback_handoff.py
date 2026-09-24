"""Server-side rescue for loopback callbacks that cannot be reached.

A CLI listening on ``http://127.0.0.1:<port>/callback`` only receives the code
when the browser is on the same machine. An agent on an instance reached over
SSH is not, so a successful authorization ends on ``ERR_CONNECTION_REFUSED``
with a valid code stranded in the URL bar.

We issue that code, so for loopback targets the 302 is replaced with a page
that delivers it to the listener itself via ``fetch`` -- keeping the
same-machine case automatic -- and shows it for pasting if that fails. Removing
the human step entirely needs the client to poll, which is ``device_flow``.

Why the fetch is not the only path
----------------------------------
That probe is a request from a public origin to a local one, which browsers
increasingly gate: Chrome preflights it and a bare CLI listener does not answer
with ``Access-Control-Allow-Private-Network``, so the request is refused before
it is made and ``no-cors`` does not exempt it. A rejected probe therefore means
"could not ask", not "nothing is listening", and a same-machine client ends up
on the paste path it never needed -- with nowhere to paste, for a client whose
CLI has no ``--code`` option.

A top-level navigation is not gated that way, being what the ordinary 302 did.
So the ways out are offered as a link as well as text to paste: one of them
fits every case, and the page no longer has to guess which.

Why success is not a dead end either
------------------------------------
The probe resolving proves only that something answered on that port, never
that the client accepted the hand-off -- ``no-cors`` yields an opaque response,
so an error status and a rejection by a listener that wants a real navigation
both look like success. Observed 2026-09-22: the page said "Connected" while
Claude Code sat at its own paste prompt, with the link hidden behind a verdict
the page was not entitled to. The ways out are therefore always on the page,
folded away rather than removed.

Clients differ in what they want pasted -- a whole callback URL, or the bare
code -- so both are offered. The code alone is useless to a client whose prompt
wants the URL, and the URL a user can see is ours, carrying the upstream code
rather than the proxy-issued one.

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
.btn{display:inline-block;padding:12px 18px;font-size:14px;font-weight:600;border-radius:8px;
  background:var(--accent);color:#fff;text-decoration:none;margin:0 0 18px}
.ok{font-size:40px;line-height:1;margin-bottom:10px;color:var(--ok)}
.spin{width:20px;height:20px;border:2px solid var(--line);border-top-color:var(--accent);
  border-radius:50%;animation:s .8s linear infinite;margin-bottom:14px}
@keyframes s{to{transform:rotate(360deg)}}
[hidden]{display:none!important}
small{color:var(--muted);font-size:13px}
details{margin-top:18px;border-top:1px solid var(--line);padding-top:14px}
details summary{cursor:pointer;font-weight:600;font-size:14px;margin-bottom:12px}
details[open] summary{margin-bottom:14px}
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
</div>
<script>
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
</script>""",
        headers={
            # The page carries an authorization code: it must not be cached by
            # the browser or anything between us and it.
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer",
        },
    )
