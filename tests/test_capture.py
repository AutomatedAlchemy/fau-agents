#!/usr/bin/env python3
"""Tests for the trace capture — including one end-to-end run against a fake
gateway, because the thing being asserted is that a *proxied* turn survives
reassembly, not just that the folder logic works."""

import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import _paths  # noqa: F401  (puts fauclaude/ on sys.path)
import capture
import proxy

REQUEST = {
    "model": "deepseek-ai/DeepSeek-V4-Flash",
    "system": [{"type": "text", "text": "You are Claude Code."}],
    "messages": [{"role": "user", "content": "hi"}],
    "tools": [{"name": "Bash", "input_schema": {"type": "object"}}],
    "stream": True,
    "metadata": {"user_id": "should-not-be-recorded"},
}

# A thinking block, a text block and a tool call, the way the gateway streams them.
UPSTREAM_EVENTS = [
    {"type": "message_start", "message": {"id": "msg_1", "model": "m",
                                          "usage": {"input_tokens": 12}}},
    {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}},
    {"type": "content_block_delta", "index": 0,
     "delta": {"type": "thinking_delta", "thinking": "let me think"}},
    {"type": "content_block_stop", "index": 0},
    {"type": "content_block_start", "index": 1, "content_block": {"type": "text"}},
    {"type": "content_block_delta", "index": 1,
     "delta": {"type": "text_delta", "text": "hello "}},
    {"type": "content_block_delta", "index": 1,
     "delta": {"type": "text_delta", "text": "there"}},
    {"type": "content_block_stop", "index": 1},
    {"type": "content_block_start", "index": 2,
     "content_block": {"type": "tool_use", "id": "tu_1", "name": "Bash"}},
    {"type": "content_block_delta", "index": 2,
     "delta": {"type": "input_json_delta", "partial_json": '{"cmd":'}},
    {"type": "content_block_delta", "index": 2,
     "delta": {"type": "input_json_delta", "partial_json": '"ls"}'}},
    {"type": "content_block_stop", "index": 2},
    {"type": "message_delta", "delta": {"stop_reason": "tool_use"},
     "usage": {"output_tokens": 34}},
    {"type": "message_stop"},
]


def records_in(directory) -> list:
    out = []
    for path in sorted(Path(directory).glob("*.jsonl")):
        out += [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return out


class TraceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.recorder = capture.Recorder(Path(self.tmp.name))
        self.addCleanup(self.tmp.cleanup)

    def _run_stream(self, events=UPSTREAM_EVENTS, request=REQUEST):
        trace = self.recorder.begin(json.dumps(request).encode(), stream=True)
        assert trace is not None
        for event in events:
            trace.on_event(event)
        trace.finish()
        return records_in(self.tmp.name)[-1]

    def test_streamed_turn_is_reassembled_into_content_blocks(self):
        record = self._run_stream()
        kinds = [b["type"] for b in record["response"]["content"]]
        self.assertEqual(kinds, ["thinking", "text", "tool_use"])
        blocks = record["response"]["content"]
        self.assertEqual(blocks[0]["thinking"], "let me think")
        self.assertEqual(blocks[1]["text"], "hello there")
        self.assertEqual(blocks[2]["input"], {"cmd": "ls"})  # parsed, not a fragment
        self.assertEqual(blocks[2]["id"], "tu_1")
        self.assertEqual(record["response"]["stop_reason"], "tool_use")
        self.assertEqual(record["response"]["usage"]["output_tokens"], 34)

    def test_request_side_keeps_what_a_finetune_needs_and_drops_the_rest(self):
        record = self._run_stream()
        self.assertEqual(record["request"]["system"][0]["text"], "You are Claude Code.")
        self.assertEqual(record["request"]["messages"], REQUEST["messages"])
        self.assertEqual(record["request"]["tools"][0]["name"], "Bash")
        self.assertNotIn("metadata", record["request"])  # user_id is not data

    def test_retries_share_a_dedup_key_and_turns_chain_by_prefix(self):
        first = self._run_stream()
        again = self._run_stream()
        self.assertEqual(first["meta"]["dedup"], again["meta"]["dedup"])

        followup = dict(REQUEST, messages=[
            REQUEST["messages"][0],
            {"role": "assistant", "content": "hello there"},
            {"role": "user", "content": "and now?"},
        ])
        third = self._run_stream(request=followup)
        self.assertNotEqual(third["meta"]["dedup"], first["meta"]["dedup"])
        self.assertEqual(third["meta"]["turns"], 3)

    def test_a_broken_turn_is_still_recorded(self):
        trace = self.recorder.begin(json.dumps(REQUEST).encode(), stream=True)
        assert trace is not None
        for event in UPSTREAM_EVENTS[:6]:
            trace.on_event(event)
        trace.fail("client disconnected")
        trace.finish()
        record = records_in(self.tmp.name)[-1]
        self.assertEqual(record["meta"]["error"], "client disconnected")
        self.assertTrue(record["response"]["content"])  # the partial turn survives

    def test_non_message_bodies_are_not_traced(self):
        self.assertIsNone(self.recorder.begin(b"not json", stream=True))
        self.assertIsNone(self.recorder.begin(b'{"input": "count tokens"}', stream=True))

    def test_stats_counts_what_was_written(self):
        self._run_stream()
        self._run_stream()
        info = capture.stats(Path(self.tmp.name))
        self.assertEqual(info["records"], 2)
        self.assertEqual(info["files"], 1)
        self.assertGreater(info["bytes"], 0)


class _FakeGateway(BaseHTTPRequestHandler):
    def log_message(self, *args, **kwargs):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        for event in UPSTREAM_EVENTS:
            self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
        self.wfile.flush()


class ProxyCaptureTest(unittest.TestCase):
    """The wiring: a real request through a real proxy lands as a real record."""

    def test_exchange_through_the_proxy_is_captured(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        upstream = ThreadingHTTPServer(("127.0.0.1", 0), _FakeGateway)
        upstream.daemon_threads = True
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        self.addCleanup(upstream.shutdown)

        recorder = capture.Recorder(Path(tmp.name))
        server, base = proxy.serve(
            f"http://127.0.0.1:{upstream.socket.getsockname()[1]}", "key",
            recorder=recorder)
        self.addCleanup(server.shutdown)

        req = urllib.request.Request(
            base + "/v1/messages", data=json.dumps(REQUEST).encode(),
            headers={"content-type": "application/json", "accept": "text/event-stream"})
        body = urllib.request.urlopen(req, timeout=10).read().decode()
        self.assertIn("thinking_delta", body)

        records = records_in(tmp.name)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["model"], REQUEST["model"])
        self.assertEqual([b["type"] for b in records[0]["response"]["content"]],
                         ["thinking", "text", "tool_use"])


if __name__ == "__main__":
    unittest.main()
