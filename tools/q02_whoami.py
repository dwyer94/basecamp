"""Phase 0 check Q-02: does `tailscale serve` tell us who is asking?

Run on the prod PC, then publish it with `tailscale serve`
(owner's walkthrough: private/phase0-q02.md):

    python tools\\q02_whoami.py

Serves one page on 127.0.0.1:8150 that shows the request headers (looking for
Tailscale-User-Login), whether the browser treats the page as secure, and a copy
button to test the clipboard. Standard library only; stop it with Ctrl+C.
"""

import html
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST, PORT = "127.0.0.1", 8150

PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Q-02 whoami</title>
<style>
 body {{ font: 16px system-ui, sans-serif; margin: 16px; background: #fff; color: #111; }}
 .ok {{ color: #0a7a2f; font-weight: 600; }} .bad {{ color: #b00020; font-weight: 600; }}
 pre {{ background: #f3f3f3; padding: 8px; overflow-x: auto; font-size: 13px; }}
 button {{ font-size: 16px; padding: 10px 16px; }}
</style></head><body>
<h1>Q-02 whoami</h1>
<p>Tailscale login: <span class="{login_class}">{login}</span></p>
<p>Tailscale name: {name}</p>
<p>Secure page (needed for the copy button): <span id="secure"></span></p>
<p><button id="copy">Copy test text</button> <span id="copied"></span></p>
<h2>All request headers</h2>
<pre>{headers}</pre>
<script>
 const s = document.getElementById("secure");
 s.textContent = window.isSecureContext ? "yes" : "no";
 s.className = window.isSecureContext ? "ok" : "bad";
 document.getElementById("copy").onclick = async () => {{
   const out = document.getElementById("copied");
   try {{
     await navigator.clipboard.writeText("Basecamp copy test");
     out.textContent = "copied"; out.className = "ok";
   }} catch (e) {{ out.textContent = "failed: " + e; out.className = "bad"; }}
 }};
</script>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        login = self.headers.get("Tailscale-User-Login")
        name = self.headers.get("Tailscale-User-Name")
        headers = json.dumps(dict(self.headers.items()), indent=2)
        body = PAGE.format(
            login=html.escape(login or "missing"),
            login_class="ok" if login else "bad",
            name=html.escape(name or "missing"),
            headers=html.escape(headers),
        ).encode()
        print(f"{self.client_address[0]}  login={login!r}  name={name!r}", flush=True)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print(f"Q-02 whoami on http://{HOST}:{PORT}  (Ctrl+C to stop)", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
