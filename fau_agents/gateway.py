"""The NHR@FAU "LLMs as a Service" gateway: where it is, where the key is,
which models it hosts. Standard library only."""

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

HOME = Path.home()
REPO_DIR = Path(__file__).resolve().parents[1]

# Anthropic shape. No /v1: Claude Code appends its own /v1/messages.
BASE_URL = "https://hub.nhr.fau.de/api/llmgw"
# OpenAI shape, for OpenCode and the model listing.
OPENAI_URL = BASE_URL + "/v1"
KEY_REQUEST_URL = "https://hpc.fau.de/request-llm-api-key/"

DEFAULT_MODEL = os.environ.get("DEFAULT_CLAUDE_FAU_LLM", "deepseek-ai/DeepSeek-V4-Flash-0731")
DEFAULT_SMALL_MODEL = os.environ.get("DEFAULT_CLAUDE_FAU_SMALL_LLM", "google/gemma-4-E4B-it")

# The listing tags embedding and OCR models with a `mode`; chat models carry none.
_NON_CHAT_MODES = {"embedding", "ocr"}


def key_from_file(path: Path) -> str:
    """First LLMAPI_KEY assignment in a .env-style file ('export' optional)."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return ""
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("export "):
            stripped = stripped[len("export "):].lstrip()
        if stripped.startswith("LLMAPI_KEY"):
            _, _, value = stripped.partition("=")
            return value.strip().strip('"').strip("'")
    return ""


def key_files() -> list:
    """Files that may hold the key, in the order they are read.

    `~/.faullm_env` is the documented place. The other two find the `.env` of a
    faullm checkout: `FAULLM_DIR` when set, else the layout the WW3 tools
    installer produces, where `NHR/` sits next to this repo.
    """
    files = [HOME / ".faullm_env"]
    if os.environ.get("FAULLM_DIR"):
        files.append(Path(os.path.expanduser(os.environ["FAULLM_DIR"])) / ".env")
    files.append(REPO_DIR.parent / "NHR" / "tools" / "faullm" / ".env")
    return files


def resolve_key() -> str:
    """LLMAPI_KEY from the environment, else from the first file that has one."""
    key = os.environ.get("LLMAPI_KEY", "").strip()
    if key:
        return key
    for path in key_files():
        key = key_from_file(path)
        if key:
            return key
    return ""


def missing_key_message(prog: str) -> str:
    return (f"{prog}: no LLMAPI_KEY. Put LLMAPI_KEY=sk-... in ~/.faullm_env "
            f"(chmod 600) or export it.\n"
            f"{' ' * len(prog)}  Request a key at {KEY_REQUEST_URL}")


def fetch_models(key: str, timeout: float = 15) -> list:
    """The gateway's /v1/models entries. Raises OSError or ValueError on failure."""
    req = urllib.request.Request(OPENAI_URL + "/models",
                                 headers={"authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    models = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(models, list):
        raise ValueError("unexpected /v1/models response")
    return [m for m in models if isinstance(m, dict) and m.get("id")]


def chat_models(models: list) -> list:
    """Drop the embedding and OCR models; an agent can only use chat models."""
    return [m for m in models if m.get("mode") not in _NON_CHAT_MODES]


def print_models(key: str, out=sys.stderr) -> list:
    """List the hosted chat models. The gateway adds and drops models without
    notice, so a stale --model is the usual failure. A failed listing is
    reported and otherwise ignored. Returns the chat models, or [] on failure."""
    try:
        models = chat_models(fetch_models(key))
    except (OSError, ValueError) as exc:
        print(f"  (model listing unavailable: {exc}; continuing)", file=out)
        return []
    print("FAU LLM gateway, hosted chat models:", file=out)
    for m in models:
        context = m.get("max_input_tokens")
        size = f"{context // 1024}k context" if isinstance(context, int) else ""
        print(f"  {m['id']:50s} {size}", file=out)
    return models
