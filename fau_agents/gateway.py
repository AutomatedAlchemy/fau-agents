"""The NHR@FAU "LLMs as a Service" gateway: where it is, where the key is,
which models it hosts, and the prompt that asks for a key the first time.
Standard library only."""

import functools
import getpass
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

HOME = Path.home()
REPO_DIR = Path(__file__).resolve().parents[1]

# Anthropic shape. No /v1: Claude Code appends its own /v1/messages.
BASE_URL = "https://hub.nhr.fau.de/api/llmgw"
# OpenAI shape, for OpenCode and the model listing.
OPENAI_URL = BASE_URL + "/v1"
KEY_REQUEST_URL = "https://hpc.fau.de/request-llm-api-key/"
# The form further down that page. The anchor is the form's WordPress id; if
# the page is rebuilt, the link still opens the page.
KEY_FORM_URL = KEY_REQUEST_URL + "#wpcf7-f47025-p47027-o1"

DEFAULT_MODEL = os.environ.get("DEFAULT_CLAUDE_FAU_LLM", "deepseek-ai/DeepSeek-V4-Flash-0731")
DEFAULT_SMALL_MODEL = os.environ.get("DEFAULT_CLAUDE_FAU_SMALL_LLM", "google/gemma-4-E4B-it")

# Models that read images, probed on 2026-10-05; the listing has no vision flag.
# For every other model fauclaude turns images into text (fauclaude/vision.py):
# DeepSeek-V4-Flash answers an image with HTTP 400, gpt-oss-120b drops it silently.
VISION_MODELS = frozenset({
    "Qwen/Qwen3.6-35B-A3B-FP8",
    "RedHatAI/Mistral-Small-3.2-24B-Instruct-2506-FP8",
    "GaleneAI/Magistral-Small-2509-FP8-Dynamic",
    "RedHatAI/gemma-4-31B-it-FP8-block",
    "google/gemma-4-E4B-it",
})
# Writes those descriptions. On a Rietveld fit plot it was the fastest of the
# five (2.5 s) and placed the peaks within half a degree.
DEFAULT_VISION_MODEL = "Qwen/Qwen3.6-35B-A3B-FP8"

# The listing tags embedding and OCR models with a `mode`; chat models carry none.
_NON_CHAT_MODES = {"embedding", "ocr"}

# The gateway's certificate chains to this root (valid to 2045). Mozilla's and
# most Linux stores carry it; a fresh Windows does not. Windows downloads such
# roots on demand for its own TLS clients, but Python only reads the Windows
# store, so every urllib call failed with CERTIFICATE_VERIFY_FAILED (Windows 11
# VM, 2026-10-05). Claude Code and OpenCode bring their own CA list.
HARICA_TLS_ECC_ROOT_CA_2021 = """\
-----BEGIN CERTIFICATE-----
MIICVDCCAdugAwIBAgIQZ3SdjXfYO2rbIvT/WeK/zjAKBggqhkjOPQQDAzBsMQsw
CQYDVQQGEwJHUjE3MDUGA1UECgwuSGVsbGVuaWMgQWNhZGVtaWMgYW5kIFJlc2Vh
cmNoIEluc3RpdHV0aW9ucyBDQTEkMCIGA1UEAwwbSEFSSUNBIFRMUyBFQ0MgUm9v
dCBDQSAyMDIxMB4XDTIxMDIxOTExMDExMFoXDTQ1MDIxMzExMDEwOVowbDELMAkG
A1UEBhMCR1IxNzA1BgNVBAoMLkhlbGxlbmljIEFjYWRlbWljIGFuZCBSZXNlYXJj
aCBJbnN0aXR1dGlvbnMgQ0ExJDAiBgNVBAMMG0hBUklDQSBUTFMgRUNDIFJvb3Qg
Q0EgMjAyMTB2MBAGByqGSM49AgEGBSuBBAAiA2IABDgI/rGgltJ6rK9JOtDA4MM7
KKrxcm1lAEeIhPyaJmuqS7psBAqIXhfyVYf8MLA04jRYVxqEU+kw2anylnTDUR9Y
STHMmE5gEYd103KUkE+bECUqqHgtvpBBWJAVcqeht6NCMEAwDwYDVR0TAQH/BAUw
AwEB/zAdBgNVHQ4EFgQUyRtTgRL+BNUW0aq8mm+3oJUZbsowDgYDVR0PAQH/BAQD
AgGGMAoGCCqGSM49BAMDA2cAMGQCMBHervjcToiwqfAircJRQO9gcS3ujwLEXQNw
SaSS6sUUiHCm0w2wqsosQJz76YJumgIwK0eaB8bRwoF8yguWGEEbo/QwCZ61IygN
nxS2PFOiTAZpffpskcYqSUXm7LcT4Tps
-----END CERTIFICATE-----
"""


@functools.lru_cache(maxsize=None)
def ssl_context() -> ssl.SSLContext:
    """The system trust store plus the gateway's root, for every gateway call."""
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(cadata=HARICA_TLS_ECC_ROOT_CA_2021)
    return ctx


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
            f"{' ' * len(prog)}  Request a key at {KEY_FORM_URL}")


def fetch_models(key: str, timeout: float = 15) -> list:
    """The gateway's /v1/models entries. Raises OSError or ValueError on failure."""
    req = urllib.request.Request(OPENAI_URL + "/models",
                                 headers={"authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=timeout, context=ssl_context()) as resp:
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


# --- First run: no key yet -----------------------------------------------------

def check_key(key: str):
    """True if the gateway accepts the key, False if it refuses it (it answers a
    wrong key with 401), None if the gateway could not be asked."""
    try:
        fetch_models(key)
    except urllib.error.HTTPError as err:
        return False if err.code in (401, 403) else None
    except (OSError, ValueError):
        return None
    return True


def save_key(key: str, path: Path = None) -> Path:
    """Write `LLMAPI_KEY=<key>` into ~/.faullm_env with mode 600. An existing
    LLMAPI_KEY line is replaced, every other line stays."""
    path = Path(path or HOME / ".faullm_env")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        prefix = "export " if stripped.startswith("export ") else ""
        if stripped[len(prefix):].lstrip().startswith("LLMAPI_KEY"):
            lines[i] = f"{prefix}LLMAPI_KEY={key}"
            break
    else:
        lines.append(f"LLMAPI_KEY={key}")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    os.chmod(path, 0o600)  # os.open leaves the mode of an existing file alone
    return path


def can_open_browser() -> bool:
    """On a Linux host without a display, webbrowser falls back to a text
    browser (lynx, w3m) that would take over this terminal."""
    if os.name == "nt" or sys.platform == "darwin":
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def send_to_request_form(prog: str) -> None:
    print(f"\nRequest a key with the form at\n  {KEY_FORM_URL}\n"
          "Use your university email address. NHR only processes requests from those.\n"
          "Under how you heard of the service, name your institute or chair:\n"
          "\"by my supervisor\" alone is not enough. NHR approves each request\n"
          f"by hand. When you have the key, start {prog} again and paste it.",
          file=sys.stderr)
    if can_open_browser():
        try:
            webbrowser.open(KEY_FORM_URL)
        except webbrowser.Error:
            pass


def ask_for_key(prog: str) -> str:
    """No key found. In a terminal, ask for one: a pasted key the gateway
    accepts is saved to ~/.faullm_env, Enter alone opens the request form.
    Returns the key, or "" when there is none (then the caller exits).
    Without a terminal it prints missing_key_message and asks nothing."""
    if sys.stdin is None or not sys.stdin.isatty():
        print(missing_key_message(prog), file=sys.stderr)
        return ""
    print(f"{prog}: no LLMAPI_KEY found. The NHR@FAU LLM gateway needs a "
          "personal key.", file=sys.stderr)
    while True:
        try:
            key = getpass.getpass("Paste your key (the input stays hidden), or press "
                                  "Enter to open the request form: ").strip()
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            return ""
        if not key:
            send_to_request_form(prog)
            return ""
        verdict = check_key(key)
        if verdict is False:
            print("The gateway refused this key. Check it and paste it again.",
                  file=sys.stderr)
            continue
        if verdict is None:
            print("  (the gateway could not be reached to check the key; saving it "
                  "anyway)", file=sys.stderr)
        try:
            path = save_key(key)
        except OSError as err:
            print(f"  (could not save the key: {err}; it holds for this session only)",
                  file=sys.stderr)
        else:
            print(f"Key saved to {path}.", file=sys.stderr)
        return key
