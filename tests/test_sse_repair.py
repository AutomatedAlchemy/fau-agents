#!/usr/bin/env python3
"""Tests for the gateway stream repair — driven by the trace actually captured
from hub.nhr.fau.de, where a block declared `text` receives a thinking delta."""

import contextlib
import io
import json
import socket
import struct
import threading
import time
import unittest
import urllib.request

import _paths  # noqa: F401  (puts fauclaude/ on sys.path)
import sse_repair


def start(i, t):
    return {"type": "content_block_start", "index": i, "content_block": {"type": t}}


def delta(i, t, **kw):
    return {"type": "content_block_delta", "index": i, "delta": {"type": t, **kw}}


def types(events):
    """[(event kind, index, block-or-delta type)] for readable assertions."""
    out = []
    for e in events:
        if e["type"] == "content_block_start":
            out.append(("start", e["index"], e["content_block"]["type"]))
        elif e["type"] == "content_block_delta":
            out.append(("delta", e["index"], e["delta"]["type"]))
        elif e["type"] == "content_block_stop":
            out.append(("stop", e["index"], None))
        else:
            out.append((e["type"], None, None))
    return out


class RepairTest(unittest.TestCase):
    def test_captured_gateway_trace_is_made_consistent(self):
        """The real failure: idx2 opens as text, then a thinking delta lands."""
        upstream = [
            {"type": "message_start"},
            start(0, "text"), delta(0, "text_delta", text=""), {"type": "content_block_stop", "index": 0},
            start(1, "thinking"), delta(1, "thinking_delta", thinking="hm"),
            {"type": "content_block_stop", "index": 1},
            start(2, "text"), delta(2, "thinking_delta", thinking="more"),
            delta(2, "text_delta", text="391"), {"type": "content_block_stop", "index": 2},
            {"type": "message_delta"}, {"type": "message_stop"},
        ]
        got = types(sse_repair.repair_events(upstream))
        for kind, index, dtype in got:
            if kind == "delta":
                block = [b for k, i, b in got if k == "start" and i == index][-1]
                self.assertEqual(sse_repair._DELTA_BLOCK_TYPE[dtype], block,
                                 f"{dtype} landed in a {block} block")

    def test_consecutive_same_type_deltas_share_one_block(self):
        upstream = [start(0, "text"), delta(0, "text_delta", text="a"),
                    {"type": "content_block_stop", "index": 0},
                    start(1, "text"), delta(1, "text_delta", text="b"),
                    {"type": "message_stop"}]
        got = types(sse_repair.repair_events(upstream))
        self.assertEqual([k for k, _i, _t in got].count("start"), 1)
        self.assertEqual(got[-1][0], "message_stop")

    def test_thinking_then_text_becomes_two_blocks_in_order(self):
        upstream = [start(0, "text"), delta(0, "thinking_delta", thinking="t"),
                    delta(0, "text_delta", text="answer"), {"type": "message_stop"}]
        got = types(sse_repair.repair_events(upstream))
        self.assertEqual(got[0], ("start", 0, "thinking"))
        self.assertIn(("start", 1, "text"), got)
        self.assertLess(got.index(("start", 0, "thinking")), got.index(("start", 1, "text")))

    def test_open_block_is_closed_before_message_stop(self):
        upstream = [start(0, "text"), delta(0, "text_delta", text="x"), {"type": "message_stop"}]
        got = types(sse_repair.repair_events(upstream))
        self.assertEqual(got[-2:], [("stop", 0, None), ("message_stop", None, None)])

    def test_tool_use_block_is_forwarded_verbatim(self):
        """Its id and name cannot be reconstructed, so it must pass through."""
        tool = {"type": "content_block_start", "index": 0,
                "content_block": {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {}}}
        upstream = [tool, delta(0, "input_json_delta", partial_json='{"a"'),
                    {"type": "content_block_stop", "index": 0}, {"type": "message_stop"}]
        got = list(sse_repair.repair_events(upstream))
        self.assertEqual(got[0]["content_block"]["id"], "toolu_1")
        self.assertEqual(got[0]["content_block"]["name"], "Bash")
        self.assertEqual(types(got)[:3],
                         [("start", 0, "tool_use"), ("delta", 0, "input_json_delta"), ("stop", 0, None)])

    def test_indices_stay_dense_after_a_tool_call(self):
        upstream = [start(0, "text"), delta(0, "thinking_delta", thinking="t"),
                    {"type": "content_block_start", "index": 1,
                     "content_block": {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}},
                    delta(1, "input_json_delta", partial_json="{}"),
                    {"type": "content_block_stop", "index": 1},
                    start(2, "text"), delta(2, "text_delta", text="done"),
                    {"type": "message_stop"}]
        indices = [i for k, i, _t in types(sse_repair.repair_events(upstream)) if k == "start"]
        self.assertEqual(indices, [0, 1, 2])

    def test_unknown_delta_kinds_are_dropped_not_misfiled(self):
        upstream = [start(0, "text"), delta(0, "citations_delta"),
                    delta(0, "text_delta", text="x"), {"type": "message_stop"}]
        got = types(sse_repair.repair_events(upstream))
        self.assertNotIn("citations_delta", [t for _k, _i, t in got])

    def test_passthrough_events_survive(self):
        upstream = [{"type": "message_start", "message": {"id": "m"}}, {"type": "ping"},
                    start(0, "text"), delta(0, "text_delta", text="x"),
                    {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
                    {"type": "message_stop"}]
        got = list(sse_repair.repair_events(upstream))
        self.assertEqual(got[0]["message"]["id"], "m")
        self.assertEqual([e["type"] for e in got if e["type"] == "ping"], ["ping"])
        self.assertEqual(got[-2]["delta"]["stop_reason"], "end_turn")

    def test_sse_bytes_are_well_formed(self):
        lines = [b'data: {"type":"content_block_start","index":0,"content_block":{"type":"text"}}',
                 b'data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"t"}}',
                 b"data: [DONE]"]
        out = b"".join(sse_repair.repair_sse(lines)).decode()
        self.assertTrue(out.startswith("event: content_block_start\ndata: {"))
        self.assertTrue(out.endswith("\n\n"))
        first = json.loads(out.split("data: ", 1)[1].split("\n", 1)[0])
        self.assertEqual(first["content_block"]["type"], "thinking")


class ClientDisconnectTest(unittest.TestCase):
    """A client resetting an idle keep-alive connection is not an error.

    socketserver's default prints a traceback to stderr for it, which lands in
    the middle of the user's TUI.
    """

    def _serve(self, handler_cls):
        server = sse_repair._Server(("127.0.0.1", 0), handler_cls)
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server.socket.getsockname()[1]

    def test_reset_connection_is_silent(self):
        port = self._serve(sse_repair._Handler)
        sock = socket.create_connection(("127.0.0.1", port))
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            sock.close()
            time.sleep(0.3)
        self.assertEqual(stderr.getvalue(), "")

    def test_other_errors_still_reported(self):
        class Boom(sse_repair._Handler):
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
