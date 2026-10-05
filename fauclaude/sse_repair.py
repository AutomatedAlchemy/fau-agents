#!/usr/bin/env python3
"""Local repair proxy for the NHR@FAU gateway's Anthropic-compatible stream.

The gateway serves `/v1/messages` in Anthropic shape, but its SSE translation
does not track which content block is currently open. Observed on
deepseek-ai/DeepSeek-V4-Flash:

    content_block_start  idx2  {"type": "text"}
    content_block_delta  idx2  {"type": "thinking_delta"}   <-- wrong block
    content_block_delta  idx2  {"type": "text_delta"}

Claude Code registered block 2 as text, gets a thinking delta for it, and
aborts the turn with `API Error: Content block is not a thinking block`. Same
class of bug as claude-code-router#1378 / PR#1356 and LiteLLM#29441: the
reasoning channel and the content channel are interleaved into one block.

Non-streaming requests are unaffected, but Claude Code always streams, so the
fix has to sit in the stream. This proxy re-derives each block's type from the
deltas themselves — the deltas are the trustworthy part — and re-emits a clean
block sequence: a delta whose type disagrees with the open block closes it and
opens a correctly typed one. Blocks are renumbered so the indices stay dense
and consecutive same-type deltas merge into one block.

Everything else (message_start/delta/stop, ping, error, tool_use blocks and
their input_json_delta) passes through untouched.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# delta type -> the content block type that delta may legally appear in
_DELTA_BLOCK_TYPE = {
    "thinking_delta": "thinking",
    "signature_delta": "thinking",
    "text_delta": "text",
    "input_json_delta": "tool_use",
}

_EMPTY_BLOCK = {
    "thinking": {"type": "thinking", "thinking": "", "signature": None},
    "text": {"type": "text", "text": ""},
}


def _event(payload: dict) -> bytes:
    return (f"event: {payload['type']}\n"
            f"data: {json.dumps(payload, separators=(',', ':'))}\n\n").encode("utf-8")


def repair_events(events):
    """Yield a protocol-clean event stream from the gateway's events.

    Pure and synchronous so it can be tested without a socket: takes an
    iterable of parsed event dicts, yields event dicts.
    """
    out_index = -1
    open_type = None          # type of the block currently open downstream
    passthrough_block = None  # upstream index of a tool_use block we forward as-is

    def close():
        nonlocal open_type
        if open_type is not None:
            yield {"type": "content_block_stop", "index": out_index}
            open_type = None

    for event in events:
        etype = event.get("type")

        if etype == "content_block_start":
            block = event.get("content_block") or {}
            if block.get("type") == "tool_use":
                # Tool calls carry an id and a name we must not invent; forward
                # the block verbatim, only renumbering it.
                yield from close()
                out_index += 1
                open_type = "tool_use"
                passthrough_block = event.get("index")
                yield {**event, "index": out_index}
            # Any other start is advisory: its declared type is exactly what the
            # gateway gets wrong, so the deltas decide instead.
            continue

        if etype == "content_block_delta":
            delta = event.get("delta") or {}
            want = _DELTA_BLOCK_TYPE.get(delta.get("type"))
            if want is None:
                continue  # unknown delta kind: nothing safe to attach it to
            if want == "tool_use":
                if open_type == "tool_use" and event.get("index") == passthrough_block:
                    yield {**event, "index": out_index}
                continue
            if want != open_type:
                yield from close()
                out_index += 1
                open_type = want
                yield {"type": "content_block_start", "index": out_index,
                       "content_block": dict(_EMPTY_BLOCK[want])}
            yield {**event, "index": out_index}
            continue

        if etype == "content_block_stop":
            # Closing is driven by the next delta's type (so split blocks merge
            # back together), except for tool_use which must close exactly.
            if open_type == "tool_use" and event.get("index") == passthrough_block:
                yield {"type": "content_block_stop", "index": out_index}
                open_type = None
                passthrough_block = None
            continue

        if etype in ("message_delta", "message_stop"):
            yield from close()

        yield event


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


def repair_sse(lines, on_event=None):
    """Byte-level wrapper: SSE lines in, repaired SSE bytes out.

    `on_event` sees each repaired event before it is encoded — that is where
    capture.Trace folds the assistant turn back together.
    """
    for event in repair_events(parse_sse(lines)):
        if on_event is not None:
            on_event(event)
        yield _event(event)


class _Handler(BaseHTTPRequestHandler):
    upstream = ""
    api_key = ""
    recorder = None  # capture.Recorder, or None when capture is off
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
            resp = urllib.request.urlopen(req, timeout=900)
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
            for chunk in repair_sse(resp, on_event=trace.on_event if trace else None):
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
            resp = urllib.request.urlopen(req, timeout=60)
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
          recorder=None):
    """Start the proxy on a background thread. Returns (server, base_url).

    `recorder` is an optional capture.Recorder; every exchange is written to it.
    """
    handler = type("Handler", (_Handler,), {"upstream": upstream, "api_key": api_key,
                                            "recorder": recorder})
    server = _Server((host, port), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    bound_host, bound_port = server.socket.getsockname()[:2]
    if bound_host == "0.0.0.0":
        bound_host = socket.gethostbyname("localhost")
    return server, f"http://{bound_host}:{bound_port}"
