#!/usr/bin/env python3
"""Tiny stdlib-only static server for the voice web client.

Serves the files in this directory (index.html and friends) on 0.0.0.0:<port>
so the same page is reachable over the LAN (desktop) and Tailscale (phone).
No dependencies beyond the Python standard library.

Also answers /models (and /v1/models) with an llama-router-shaped JSON blob
reporting whether the voice-agent websocket (127.0.0.1:8765) is listening -
this lets the WigiDash LLM Launcher widget poll it like any other backend.
Checked passively via /proc/net/tcp{,6} (no connection made) so polling
doesn't spam the voice-agent websocket server with bare-TCP-connect noise.
"""

import argparse
import json
import os
import ssl
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
VOICE_AGENT_PORT = 8765
TCP_LISTEN_STATE = "0A"
# Dynamic endpoints: their body reflects live state, so they must never be stored.
MODEL_PATHS = ("/models", "/v1/models")


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=HERE, **kwargs)

    def do_GET(self):
        if urlsplit(self.path).path in MODEL_PATHS:
            self._serve_models()
            return
        super().do_GET()

    def do_HEAD(self):
        if urlsplit(self.path).path in MODEL_PATHS:
            self._serve_models(head=True)
            return
        super().do_HEAD()

    def _serve_models(self, head=False):
        if self._voice_agent_listening():
            status = 200
            body = json.dumps({
                "object": "list",
                "data": [{"id": "voice-agent", "status": {"value": "loaded"}}],
            }).encode("utf-8")
        else:
            status = 503
            body = json.dumps({"error": "voice-agent unreachable"}).encode("utf-8")

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()  # also sends Cache-Control: no-store
        if not head:
            self.wfile.write(body)

    @staticmethod
    def _voice_agent_listening():
        port_hex = "%04X" % VOICE_AGENT_PORT
        for path in ("/proc/net/tcp", "/proc/net/tcp6"):
            try:
                with open(path) as f:
                    next(f)  # header line
                    for line in f:
                        fields = line.split()
                        local_port = fields[1].split(":")[1]
                        state = fields[3]
                        if local_port == port_hex and state == TCP_LISTEN_STATE:
                            return True
            except OSError:
                continue
        return False

    def end_headers(self):
        # The dynamic endpoints must never be stored. Static assets get
        # "no-cache", which is NOT "don't cache" -- it means "revalidate before
        # use", so SimpleHTTPRequestHandler's own Last-Modified/If-Modified-Since
        # handling answers a repeat load with a cheap 304 instead of resending
        # the file. That keeps the never-serve-stale-during-iteration property
        # the blanket "no-store" was there for, without re-downloading the
        # multi-megabyte avatar GLB on every page load. Deliberately not
        # "max-age": a timed cache is exactly what would serve something stale.
        cache = "no-store" if urlsplit(self.path).path in MODEL_PATHS else "no-cache"
        self.send_header("Cache-Control", cache)
        # Clickjacking: the cockpit is a full voice/persona/brain control
        # surface, so an invisible iframe over it (framing the page and
        # tricking the user into taps that land on the real hidden controls)
        # is a live threat, not a defense-in-depth nicety. Both headers on
        # every response for the two browser families that honor one or the
        # other.
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        super().end_headers()

    def log_message(self, fmt, *args):
        # Compact one-line access log.
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--certfile", help="TLS cert (PEM); with --keyfile, serves HTTPS")
    ap.add_argument("--keyfile", help="TLS private key (PEM)")
    args = ap.parse_args()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    scheme = "http"
    if args.certfile and args.keyfile:
        # HTTPS lets the LAN origin be a secure context so the lip-sync
        # AudioWorklet runs without Tailscale. stdlib ssl, no dependency.
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=args.certfile, keyfile=args.keyfile)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        scheme = "https"
    print(f"Serving {HERE} on {scheme}://{args.host}:{args.port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
