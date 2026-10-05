#!/usr/bin/env python3
"""fauopencode: OpenCode on the NHR@FAU "LLMs as a Service" gateway, with your
skills loaded for that session only.

OpenCode talks to the gateway's OpenAI-compatible endpoint
(`https://hub.nhr.fau.de/api/llmgw/v1`), whose stream needs no repair. The
whole setup goes in through `OPENCODE_CONFIG_CONTENT`, which OpenCode merges
last and keeps for this process only:

- a provider `fau` whose models come from the gateway's live `/v1/models`
  listing, with the context sizes it reports;
- `enabled_providers: ["fau"]`, so the model picker shows only the gateway;
- `share: "disabled"`, so no session is uploaded to opencode.ai by accident;
- `skills.paths` pointing at the skills staged for this profile.

The key reaches OpenCode as `LLMAPI_KEY` in its environment; the config only
names the variable. Your own `~/.config/opencode` stays untouched.

`--profile` names the skill set: `~/.config/<profile>/skills` is read, the same
directory fauclaude reads. ww3opencode is this launcher with
`--profile ww3claude`.
"""

import json
import sys

# --advertise must answer before any heavy import (installer 5 s timeout).
PARENT_METADATA = {
    "name": "fauopencode",
    "capability": "agent",
    "domain": "llm",
    "desktop_file": "fauopencode.desktop",
    "icon": "utilities-terminal",
    "desc": "OpenCode on the free NHR@FAU LLM gateway, with your skills loaded "
            "for that session only",
    "terminal": True,
    "args": [],
    "tags": ["CLI"],
    "alias": "fauopencode",
}

if "--advertise" in sys.argv:
    print(json.dumps([PARENT_METADATA]))
    sys.exit(0)

import argparse
import os
import shutil
import subprocess
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR.parent) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR.parent))

from fau_agents import gateway, install, skills  # noqa: E402

HOME = Path.home()
DEFAULT_PROFILE = "fauclaude"
PROVIDER = "fau"
# The gateway reports each model's context size as both its input and its
# output limit. Asking for that much output would leave no room for the
# prompt, so the output limit is capped.
OUTPUT_CAP = 32768
# Used when the listing fails or a model is missing from it.
FALLBACK_LIMIT = {"context": 32768, "output": 8192}


def find_opencode() -> str:
    """`opencode` on PATH, else where its install script puts it."""
    found = shutil.which("opencode")
    if found:
        return found
    exe = "opencode.exe" if os.name == "nt" else "opencode"
    candidate = HOME / ".opencode" / "bin" / exe
    return str(candidate) if candidate.exists() else ""


def model_entry(listing: dict) -> dict:
    context = listing.get("max_input_tokens")
    output = listing.get("max_output_tokens")
    if not isinstance(context, int) or context <= 0:
        return {"name": listing["id"], "limit": dict(FALLBACK_LIMIT), "tool_call": True}
    if not isinstance(output, int) or output <= 0:
        output = FALLBACK_LIMIT["output"]
    return {"name": listing["id"],
            "limit": {"context": context, "output": min(output, OUTPUT_CAP, context)},
            "tool_call": True}


def provider_models(listing: list, model: str, small_model: str) -> dict:
    """{model id: OpenCode model entry} for every hosted chat model. The chosen
    models are added with fallback limits when the listing lacks them."""
    models = {m["id"]: model_entry(m) for m in gateway.chat_models(listing)}
    for wanted in (model, small_model):
        models.setdefault(wanted, model_entry({"id": wanted}))
    return models


def build_config(listing: list, model: str, small_model: str, skills_dir: Path) -> dict:
    return {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            PROVIDER: {
                "npm": "@ai-sdk/openai-compatible",
                "name": "NHR@FAU LLM gateway",
                "options": {"baseURL": gateway.OPENAI_URL,
                            "apiKey": "{env:LLMAPI_KEY}"},
                "models": provider_models(listing, model, small_model),
            },
        },
        "enabled_providers": [PROVIDER],
        "model": f"{PROVIDER}/{model}",
        "small_model": f"{PROVIDER}/{small_model}",
        "share": "disabled",
        "skills": {"paths": [str(skills_dir)]},
    }


def launch_env(key: str, config: dict) -> dict:
    env = os.environ.copy()
    env["LLMAPI_KEY"] = key
    env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config)
    return env


def stage_skills(profile: str, extra=(), root=None) -> tuple:
    """Stage the profile's skills. Returns (staging dir, [skill names])."""
    dest = skills.staged_dir("opencode", profile)
    found = skills.discover(skills.skill_roots(profile, extra, root))
    return dest, skills.stage(dest, found)


def launch(model: str, small_model: str, passthrough: list, *,
           profile: str = DEFAULT_PROFILE, extra=(), dry_run: bool = False) -> int:
    key = gateway.resolve_key()
    if not key:
        print(gateway.missing_key_message(profile.replace("claude", "opencode")),
              file=sys.stderr)
        return 1
    exe = find_opencode()
    if not exe and not dry_run:
        print("opencode is not installed: see https://opencode.ai/docs/ "
              "(curl -fsSL https://opencode.ai/install | bash)", file=sys.stderr)
        return 1
    skills_dir, names = stage_skills(profile, extra)
    print(f"skills (this session only): {', '.join(names) or 'none found'}", file=sys.stderr)
    try:
        listing = gateway.fetch_models(key)
    except (OSError, ValueError) as exc:
        listing = []
        print(f"  (model listing unavailable: {exc}; offering only {model})",
              file=sys.stderr)
    if listing and model not in {m["id"] for m in listing}:
        print(f"  warning: {model} is not in the gateway's listing", file=sys.stderr)
    config = build_config(listing, model, small_model, skills_dir)
    print(f"  -> starting OpenCode with: {model}", file=sys.stderr)

    argv = [exe or "opencode", *passthrough]
    if dry_run:
        print(json.dumps(config, indent=2))
        print("[dry-run] would exec:", " ".join(argv))
        return 0
    env = launch_env(key, config)
    if os.name == "nt":
        return subprocess.call(argv, env=env)
    os.execve(exe, argv, env)  # replaces this process
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="fauopencode",
        description="OpenCode on the NHR@FAU LLM gateway, with your skills loaded "
                    "for this session only. Unknown arguments and anything after "
                    "-- go to opencode (e.g. `fauopencode run \"...\"`).")
    ap.add_argument("--install", action="store_true", help="register the alias")
    ap.add_argument("--remove", action="store_true", help="unregister the alias")
    ap.add_argument("--model", default=gateway.DEFAULT_MODEL,
                    help=f"gateway model to run (default: {gateway.DEFAULT_MODEL})")
    ap.add_argument("--small-model", default=gateway.DEFAULT_SMALL_MODEL,
                    help="model for titles and summaries "
                         f"(default: {gateway.DEFAULT_SMALL_MODEL})")
    ap.add_argument("--profile", default=DEFAULT_PROFILE,
                    help="skill set: reads ~/.config/<profile>/skills "
                         f"(default: {DEFAULT_PROFILE})")
    ap.add_argument("--skill-root", action="append", default=[], metavar="DIR",
                    help="also stage the skills under DIR (repeatable)")
    ap.add_argument("--list-skills", action="store_true",
                    help="show which skills would be staged, then exit")
    ap.add_argument("--models", action="store_true",
                    help="list the gateway's chat models, then exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the generated config and the opencode invocation "
                         "instead of running it")
    return ap


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    known, passthrough = build_parser().parse_known_args(argv)
    if "--" in passthrough:
        passthrough.remove("--")
    if known.install or known.remove:
        return install.install_or_remove(PARENT_METADATA, __file__, known.remove)
    if known.list_skills:
        roots = skills.skill_roots(known.profile, known.skill_root)
        skills.print_listing(roots, skills.discover(roots))
        return 0
    if known.models:
        key = gateway.resolve_key()
        if not key:
            print(gateway.missing_key_message("fauopencode"), file=sys.stderr)
            return 1
        return 0 if gateway.print_models(key, out=sys.stdout) else 1
    return launch(known.model, known.small_model, passthrough, profile=known.profile,
                  extra=known.skill_root, dry_run=known.dry_run)


if __name__ == "__main__":
    sys.exit(main())
