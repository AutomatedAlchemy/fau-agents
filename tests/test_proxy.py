#!/usr/bin/env python3
"""Tests for the capture proxy: the stream passes through unchanged, capture
sees the parsed events, a client hanging up stays quiet."""

import contextlib
import io
import socket
import struct
import threading
import time
import unittest
import urllib.request

import _paths  # noqa: F401  (puts fauclaude/ on sys.path)
import proxy

LINES = [
    b'event: content_block_start\n',
    b'data: {"type":"content_block_start","index":0,"content_block":{"type":"thinking"}}\n',
    b'\n',
    b'data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"t"}}\n',
    b'\n',
    b': keep-alive comment\n',
    b'data: [DONE]\n',
]


class RelayTest(unittest.TestCase):
    def test_bytes_pass_through_unchanged(self):
        self.assertEqual(list(proxy.relay_sse(LINES)), LINES)

    def test_on_event_sees_each_parsed_event(self):
        seen = []
        out = list(proxy.relay_sse(LINES, on_event=seen.append))
        self.assertEqual(out, LINES)
        self.assertEqual([e["type"] for e in seen],
                         ["content_block_start", "content_block_delta"])
        self.assertEqual(seen[1]["delta"]["thinking"], "t")

    def test_done_comments_and_bad_json_are_not_events(self):
        lines = [b"data: [DONE]\n", b": ping\n", b"data: {not json\n", b"data:\n"]
        seen = []
        self.assertEqual(list(proxy.relay_sse(lines, on_event=seen.append)), lines)
        self.assertEqual(seen, [])


class ClientDisconnectTest(unittest.TestCase):
    """A client resetting an idle keep-alive connection is not an error.

    socketserver's default prints a traceback to stderr for it, which lands in
    the middle of the user's TUI.
    """

    def _serve(self, handler_cls):
        server = proxy._Server(("127.0.0.1", 0), handler_cls)
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server.socket.getsockname()[1]

    def test_reset_connection_is_silent(self):
        port = self._serve(proxy._Handler)
        sock = socket.create_connection(("127.0.0.1", port))
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            sock.close()
            time.sleep(0.3)
        self.assertEqual(stderr.getvalue(), "")

    def test_other_errors_still_reported(self):
        class Boom(proxy._Handler):
            def do_GET(self):
                raise ValueError("visible")

        port = self._serve(Boom)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/x", timeout=5).read()
            except OSError:
                pass
            time.sleep(0.3)
        self.assertIn("ValueError", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
