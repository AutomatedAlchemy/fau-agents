#!/usr/bin/env python3
"""Images as text, for gateway models that cannot read them.

Claude Code sends every image the agent opens (a PNG through Read, a pasted
screenshot) to the model. DeepSeek-V4-Flash answers with HTTP 400 "not a
multimodal model", and since the image stays in the history, every later turn
of the session fails the same way. gpt-oss-120b drops the image and answers as
if it had seen it.

The proxy passes each request body through `Describer.rewrite`. When the
request's model is not in gateway.VISION_MODELS, every image block becomes a
text block: a note that this model cannot see images, then a description by a
vision model. Descriptions are cached by image hash, because Claude Code sends
the whole history again on every turn.
"""

from __future__ import annotations

import hashlib
import json
import threading
import urllib.request

PROMPT = (
    "You describe an image for a language model that cannot see it. Be factual and "
    "complete. Transcribe every piece of text: titles, axis labels, tick values, legend "
    "entries, numbers. For a plot, describe each curve or series, where it peaks or "
    "changes, and how the series compare (for example observed vs calculated, and the "
    "size and position of any residual). Point out anything that looks wrong or "
    "unusual. Do not guess beyond what is visible. Plain text, no preamble.")


def _tool_paths(messages: list) -> dict:
    """tool_use id -> the file that tool call opened, as a hint for the describer."""
    paths = {}
    for msg in messages:
        content = msg.get("content") if isinstance(msg, dict) else None
        for block in content if isinstance(content, list) else ():
            if isinstance(block, dict) and block.get("type") == "tool_use":
                args = block.get("input")
                if isinstance(args, dict) and args.get("file_path"):
                    paths[block.get("id")] = str(args["file_path"])
    return paths


class Describer:
    """Rewrites request bodies for models outside `vision_models`."""

    def __init__(self, key: str, model: str, vision_models=frozenset(), url: str = "",
                 context=None, timeout: float = 120):
        self.key = key
        self.model = model
        self.vision_models = frozenset(vision_models)
        self.url = url  # OpenAI-shape chat/completions
        self.context = context
        self.timeout = timeout
        self._cache = {}
        self._lock = threading.Lock()

    def rewrite(self, body: bytes) -> bytes:
        """The request body with images as text, or `body` itself if nothing changed."""
        try:
            data = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return body
        if not isinstance(data, dict) or data.get("model") in self.vision_models:
            return body
        messages = data.get("messages")
        if not isinstance(messages, list):
            return body
        model = str(data.get("model"))
        paths = _tool_paths(messages)
        changed = False
        for msg in messages:
            content = msg.get("content") if isinstance(msg, dict) else None
            if not isinstance(content, list):
                continue
            for i, block in enumerate(content):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "image":
                    content[i] = self._as_text(block, None, model)
                    changed = True
                elif block.get("type") == "tool_result" and isinstance(block.get("content"), list):
                    inner = block["content"]
                    for j, sub in enumerate(inner):
                        if isinstance(sub, dict) and sub.get("type") == "image":
                            inner[j] = self._as_text(sub, paths.get(block.get("tool_use_id")), model)
                            changed = True
        return json.dumps(data, ensure_ascii=False).encode("utf-8") if changed else body

    def _as_text(self, block: dict, path, model: str) -> dict:
        name = f"Image {path}" if path else "Image"
        source = block.get("source") or {}
        data = source.get("data") if source.get("type") == "base64" else None
        if not data:
            return {"type": "text", "text": f"[{name}: not shown, {model} cannot read images.]"}
        digest = hashlib.sha256(data.encode("ascii", "replace")).hexdigest()
        with self._lock:
            text = self._cache.get(digest)
        if text is None:
            try:
                text = self.describe(data, source.get("media_type") or "image/png", path)
            except (OSError, ValueError, KeyError, IndexError, TypeError) as err:
                # Not cached: the next turn tries again.
                return {"type": "text", "text": (
                    f"[{name}: not shown, {model} cannot read images, and the description "
                    f"by {self.model} failed ({err}). Work from the data the image was "
                    f"made from instead.]")}
            with self._lock:
                self._cache[digest] = text
        return {"type": "text", "text": (
            f"[{name}: {model} cannot see images, so {self.model} described it.]\n{text}")}

    def describe(self, data: str, media_type: str, path=None) -> str:
        """One description from the vision model (OpenAI shape, thinking off)."""
        prompt = PROMPT + (f"\nThe agent opened this file: {path}" if path else "")
        body = {
            "model": self.model,
            "max_tokens": 2000,
            # Qwen3.6 thinks by default. Through the Anthropic shape that cannot be
            # turned off, and in a test it spent the whole budget before describing.
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{data}"}},
                {"type": "text", "text": prompt}]}],
        }
        req = urllib.request.Request(
            self.url, data=json.dumps(body).encode(), method="POST",
            headers={"content-type": "application/json", "authorization": f"Bearer {self.key}"})
        with urllib.request.urlopen(req, timeout=self.timeout, context=self.context) as resp:
            out = json.loads(resp.read())
        text = (out["choices"][0]["message"].get("content") or "").strip()
        if not text:
            raise ValueError("empty description")
        return text
