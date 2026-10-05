#!/usr/bin/env python3
"""Local proxy between Claude Code and the NHR@FAU gateway, for `--capture`.

It runs only while capture is on. Each request goes on to the gateway, and the
response comes back byte for byte. On the way, the parsed events go to
capture.Trace, which writes the exchange as one JSONL record.

Until 2026-10-05 this proxy also repaired the stream. The gateway's Anthropic
translation opened a block as `text` and then sent `thinking_delta`s into it,
and Claude Code aborted the turn with `API Error: Content block is not a
thinking block` (8 of 10 turns on DeepSeek-V4-Flash at xhigh, 2026-08-18).
Rechecks found 0 of 25 direct turns broken on 2026-08-24 and 0 of 10 Claude
Code sessions on 2026-10-05, so the repair went. If the error comes back, the
repair is in fauclaude/sse_repair.py at fau-agents 84edab9.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def parse_sse(lines):
    """SSE lines in, parsed event dicts out (non-data lines and [DONE] dropped)."""
    for raw in lines:
        line = raw.decode("utf-8", "replace").strip() if isinstance(raw, bytes) else raw.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            yield json.loads(payload)
        except json.JSONDecodeError:
            continue


def relay_sse(lines, on_event=None):
    """The gateway's SSE lines, passed through unchanged.

    `on_event` sees each parsed event on the way. That is where capture.Trace
    folds the assistant turn back together.
    """
    for raw in lines:
        if on_event is not None:
            for event in parse_sse([raw]):
                on_event(event)
        yield raw


class _Handler(BaseHTTPRequestHandler):
    upstream = ""
    api_key = ""
    recorder = None  # capture.Recorder, or None when capture is off
    context = None  # ssl.SSLContext for the gateway, or None for the default
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):  # keep the launcher's stderr for the user
        pass

    def handle_one_request(self):
        """Swallow the client hanging up on an idle keep-alive connection.

        Claude Code keeps the connection open after a turn and resets it when
        it tears down its pool, so the thread waiting in `readline()` for the
        next request gets ECONNRESET. socketserver has no handler for that and
        dumps a full traceback into the launcher's stderr, straight through the
        user's TUI. Nothing is lost — the turn already finished.
        """
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        trace = self.recorder.begin(body, stream=True) if self.recorder else None
        req = urllib.request.Request(
            self.upstream + self.path, data=body, method="POST",
            headers={"content-type": "application/json",
                     "accept": self.headers.get("accept", "application/json"),
                     "authorization": f"Bearer {self.api_key}",
                     "x-api-key": self.api_key,
                     "anthropic-version": self.headers.get("anthropic-version", "2023-06-01")})
        try:
            resp = urllib.request.urlopen(req, timeout=900, context=self.context)
        except urllib.error.HTTPError as err:  # forward the gateway's own errors
            payload = err.read()
            if trace:
                trace.fail(f"HTTP {err.code}: {payload[:400].decode('utf-8', 'replace')}")
                trace.finish()
            self.send_response(err.code)
            self.send_header("content-type", err.headers.get("content-type", "application/json"))
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        except OSError as err:
            if trace:
                trace.fail(str(err))
                trace.finish()
            self.send_error(502, str(err))
            return

        is_stream = "text/event-stream" in (resp.headers.get("content-type") or "")
        if not is_stream:
            payload = resp.read()
            if trace:
                trace.stream = False
                try:
                    trace.set_response(json.loads(payload))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    trace.fail("non-JSON body")
                trace.finish()
            self.send_response(resp.status)
            self.send_header("content-type", resp.headers.get("content-type", "application/json"))
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        self.send_response(resp.status)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("transfer-encoding", "chunked")
        self.end_headers()
        try:
            for chunk in relay_sse(resp, on_event=trace.on_event if trace else None):
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError):
            # Claude Code hung up (ctrl-C, /clear). The partial turn is still
            # worth keeping, so fall through to the capture below.
            if trace:
                trace.fail("client disconnected")
        finally:
            if trace:
                trace.finish()

    def do_GET(self):
        req = urllib.request.Request(
            self.upstream + self.path,
            headers={"authorization": f"Bearer {self.api_key}", "x-api-key": self.api_key})
        try:
            resp = urllib.request.urlopen(req, timeout=60, context=self.context)
            payload, status = resp.read(), resp.status
        except urllib.error.HTTPError as err:
            payload, status = err.read(), err.code
        except OSError as err:
            self.send_error(502, str(err))
            return
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class _Server(ThreadingHTTPServer):
    """ThreadingHTTPServer that does not narrate a dropped connection.

    `handle_error` lives on the server, not the handler, and its default prints
    a traceback to stderr. A client disconnect is normal here and must not
    reach the user's terminal; anything else still does.
    """

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


def serve(upstream: str, api_key: str, host: str = "127.0.0.1", port: int = 0,
          recorder=None, context=None):
    """Start the proxy on a background thread. Returns (server, base_url).

    `recorder` is an optional capture.Recorder; every exchange is written to it.
    `context` is the SSLContext for the gateway (gateway.ssl_context()).
    """
    handler = type("Handler", (_Handler,), {"upstream": upstream, "api_key": api_key,
                                            "recorder": recorder, "context": context})
    server = _Server((host, port), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    bound_host, bound_port = server.socket.getsockname()[:2]
    if bound_host == "0.0.0.0":
        bound_host = socket.gethostbyname("localhost")
    return server, f"http://{bound_host}:{bound_port}"
