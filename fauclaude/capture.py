#!/usr/bin/env python3
"""Capture every exchange that passes through the repair proxy, as training data.

The proxy already sits between Claude Code and the gateway and sees both
directions in full, so it is the only place in this stack where a complete
`(prompt, completion)` pair exists: the request body carries the system prompt,
the whole message history and the tool schemas; the repaired event stream
carries the assistant turn that answered it. Claude Code's own transcripts in
`~/.claude-fau/projects/` are the *rendered* conversation, not the request —
they do not contain the system prompt or the tool definitions, so they cannot
be replayed as a fine-tuning sample.

One JSONL record per HTTP exchange, appended to a day file:

    {"ts": ..., "model": ..., "stream": true,
     "request":  {"system": ..., "messages": [...], "tools": [...], ...},
     "response": {"content": [...], "stop_reason": ..., "usage": {...}},
     "meta": {"duration_s": ..., "dedup": "<sha256 of the request>", ...}}

`request.messages` is the *full* history Claude Code resent that turn, so
consecutive records share a growing prefix. That is the natural SFT shape —
every record is one supervised example whose target is `response.content` — but
it means the file grows superlinearly with conversation length. Filter on
`meta.dedup` (identical requests, i.e. retries) and, for a per-conversation
view, on `meta.prefix` (the hash of everything *before* the last message, which
chains a conversation's turns together).

Failed exchanges are recorded too, with `response: null` and `meta.error` — a
turn the gateway broke is exactly the sample worth looking at afterwards.

Nothing leaves the machine: this writes to a local directory (default
`~/.claude-fau/traces/`, outside any checkout) and is announced at launch.
The content is whatever the session saw, including file contents and paths, so
treat the directory as being as sensitive as the projects you use it on.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path

DEFAULT_DIR = Path(os.path.expanduser(
    os.environ.get("CLAUDE_FAU_CAPTURE_DIR", "~/.claude-fau/traces")))

# Request keys worth keeping. `metadata` (Claude Code's user_id) and the auth
# headers are deliberately not among them.
_REQUEST_KEYS = ("model", "system", "messages", "tools", "tool_choice",
                 "max_tokens", "temperature", "top_p", "top_k",
                 "stop_sequences", "thinking")


def _sha(obj) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":"),
                   default=str).encode("utf-8")).hexdigest()[:16]


class Trace:
    """One in-flight exchange. Accumulates the assistant turn, then writes it."""

    def __init__(self, recorder: "Recorder", request: dict, stream: bool):
        self.recorder = recorder
        self.request = request
        self.stream = stream
        self.started = time.time()
        self.response = None
        self.error = None
        self._blocks = {}   # out-index -> partial content block
        self._order = []
        self._message = {}

    # --- streaming ---------------------------------------------------------

    def on_event(self, event: dict) -> None:
        """Fold one *repaired* event into the assistant message being built.

        Repaired, not raw, on purpose: those are the events Claude Code
        actually consumed, so the captured turn is the one the session saw.
        """
        etype = event.get("type")
        if etype == "message_start":
            message = event.get("message") or {}
            self._message.update({k: message[k] for k in ("id", "model", "usage")
                                  if k in message})
        elif etype == "content_block_start":
            index = event.get("index")
            block = dict(event.get("content_block") or {})
            if block.get("type") == "tool_use":
                block["input"] = ""  # rebuilt from input_json_delta below
            self._blocks[index] = block
            self._order.append(index)
        elif etype == "content_block_delta":
            block = self._blocks.get(event.get("index"))
            delta = event.get("delta") or {}
            if block is None:
                return
            kind = delta.get("type")
            if kind == "text_delta":
                block["text"] = block.get("text", "") + delta.get("text", "")
            elif kind == "thinking_delta":
                block["thinking"] = block.get("thinking", "") + delta.get("thinking", "")
            elif kind == "signature_delta":
                block["signature"] = delta.get("signature")
            elif kind == "input_json_delta":
                block["input"] = block.get("input", "") + delta.get("partial_json", "")
        elif etype == "message_delta":
            delta = event.get("delta") or {}
            for key in ("stop_reason", "stop_sequence"):
                if key in delta:
                    self._message[key] = delta[key]
            if "usage" in event:
                self._message["usage"] = {**self._message.get("usage", {}),
                                          **event["usage"]}

    def _assembled(self) -> dict:
        content = []
        for index in self._order:
            block = dict(self._blocks[index])
            if block.get("type") == "tool_use" and isinstance(block.get("input"), str):
                try:
                    block["input"] = json.loads(block["input"] or "{}")
                except json.JSONDecodeError:
                    pass  # keep the raw fragment; a truncated turn is still data
            content.append(block)
        return {**self._message, "content": content}

    # --- non-streaming -----------------------------------------------------

    def set_response(self, payload: dict) -> None:
        self.response = {k: payload[k] for k in
                         ("id", "model", "content", "stop_reason", "stop_sequence", "usage")
                         if k in payload}

    def fail(self, error: str) -> None:
        self.error = error

    # --- finish ------------------------------------------------------------

    def finish(self) -> None:
        response = self.response
        if response is None and (self._order or self._message):
            response = self._assembled()
        messages = self.request.get("messages") or []
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(self.started)),
            "model": self.request.get("model"),
            "stream": self.stream,
            "request": {k: self.request[k] for k in _REQUEST_KEYS if k in self.request},
            "response": response,
            "meta": {
                "duration_s": round(time.time() - self.started, 3),
                "dedup": _sha(self.request.get("messages")),
                "prefix": _sha(messages[:-1]),
                "turns": len(messages),
                **({"error": self.error} if self.error else {}),
            },
        }
        self.recorder.write(record)


class Recorder:
    """Append-only JSONL sink, one file per day, safe across proxy threads."""

    def __init__(self, directory: Path = None):
        self.dir = Path(directory or DEFAULT_DIR).expanduser()
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.count = 0

    def path_for(self, when: float = None) -> Path:
        return self.dir / (time.strftime("%Y-%m-%d", time.localtime(when)) + ".jsonl")

    def begin(self, body: bytes, stream: bool) -> Trace | None:
        try:
            request = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None  # not a messages request; nothing to learn from it
        if not isinstance(request, dict) or "messages" not in request:
            return None
        return Trace(self, request, stream)

    def write(self, record: dict) -> None:
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            try:
                with self.path_for().open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
                self.count += 1
            except OSError:
                pass  # capture must never take a live session down


def stats(directory: Path = None) -> dict:
    """{files, records, bytes, dir} — what --traces prints."""
    path = Path(directory or DEFAULT_DIR).expanduser()
    files = sorted(path.glob("*.jsonl")) if path.exists() else []
    records = 0
    size = 0
    for f in files:
        size += f.stat().st_size
        with f.open("rb") as fh:
            records += sum(1 for line in fh if line.strip())
    return {"dir": path, "files": len(files), "records": records, "bytes": size}
