#!/usr/bin/env python3
"""Tests for the image fallback: images become text for text-only models, and
the proxy sends the rewritten body on."""

import json
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import _paths  # noqa: F401  (puts fauclaude/ on sys.path)
import proxy
import vision

PNG = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0K"}}


def request(model, content):
    """A Claude Code-shaped turn: Read of a PNG, then its tool_result."""
    return {"model": model, "max_tokens": 100, "messages": [
        {"role": "user", "content": "plot it"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "tu1", "name": "Read",
             "input": {"file_path": "C:/XRD/refine/fit.png"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu1", "content": content}]},
    ]}


class RewriteTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.describer = vision.Describer("k", "seeing-m", {"seeing-m"})
        self.describer.describe = self._describe

    def _describe(self, data, media_type, path=None):
        self.calls.append((data, media_type, path))
        return "A fit plot with residuals."

    def test_image_in_a_tool_result_becomes_text(self):
        body = json.dumps(request("blind-m", [PNG, {"type": "text", "text": "note"}])).encode()
        out = json.loads(self.describer.rewrite(body))
        blocks = out["messages"][2]["content"][0]["content"]
        self.assertEqual([b["type"] for b in blocks], ["text", "text"])
        self.assertIn("blind-m cannot see images, so seeing-m described it", blocks[0]["text"])
        self.assertIn("A fit plot with residuals.", blocks[0]["text"])
        self.assertEqual(blocks[1]["text"], "note")
        self.assertEqual(self.calls, [("iVBORw0K", "image/png", "C:/XRD/refine/fit.png")])

    def test_pasted_image_becomes_text(self):
        body = json.dumps({"model": "blind-m", "messages": [
            {"role": "user", "content": [PNG, {"type": "text", "text": "what is this?"}]}]}).encode()
        out = json.loads(self.describer.rewrite(body))
        self.assertEqual([b["type"] for b in out["messages"][0]["content"]], ["text", "text"])
        self.assertEqual(self.calls[0][2], None)

    def test_vision_model_and_imageless_bodies_pass_unchanged(self):
        seeing = json.dumps(request("seeing-m", [PNG])).encode()
        self.assertIs(self.describer.rewrite(seeing), seeing)
        plain = json.dumps(request("blind-m", "file text")).encode()
        self.assertIs(self.describer.rewrite(plain), plain)
        self.assertIs(self.describer.rewrite(b"not json"), b"not json")
        self.assertEqual(self.calls, [])

    def test_same_image_is_described_once(self):
        """Claude Code resends the history every turn."""
        body = json.dumps(request("blind-m", [PNG])).encode()
        first = self.describer.rewrite(body)
        self.assertEqual(self.describer.rewrite(body), first)
        self.assertEqual(len(self.calls), 1)

    def test_failed_description_leaves_a_note_and_retries_next_turn(self):
        def fail(*_args):
            self.calls.append("fail")
            raise OSError("timed out")

        self.describer.describe = fail
        body = json.dumps(request("blind-m", [PNG])).encode()
        text = json.loads(self.describer.rewrite(body))["messages"][2]["content"][0]["content"][0]["text"]
        self.assertIn("cannot read images", text)
        self.assertIn("timed out", text)
        self.describer.rewrite(body)
        self.assertEqual(self.calls, ["fail", "fail"])


class _EchoGateway(BaseHTTPRequestHandler):
    """Answers with the request body it received."""

    def log_message(self, *args, **kwargs):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ProxyVisionTest(unittest.TestCase):
    def test_gateway_receives_the_rewritten_body(self):
        upstream = ThreadingHTTPServer(("127.0.0.1", 0), _EchoGateway)
        upstream.daemon_threads = True
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        self.addCleanup(upstream.shutdown)

        describer = vision.Describer("k", "seeing-m", {"seeing-m"})
        describer.describe = lambda *_args: "A fit plot."
        server, base = proxy.serve(f"http://127.0.0.1:{upstream.socket.getsockname()[1]}",
                                   "key", vision=describer)
        self.addCleanup(server.shutdown)

        req = urllib.request.Request(
            base + "/v1/messages", data=json.dumps(request("blind-m", [PNG])).encode(),
            headers={"content-type": "application/json"})
        seen = json.loads(urllib.request.urlopen(req, timeout=10).read())
        block = seen["messages"][2]["content"][0]["content"][0]
        self.assertEqual(block["type"], "text")
        self.assertIn("A fit plot.", block["text"])


if __name__ == "__main__":
    unittest.main()
