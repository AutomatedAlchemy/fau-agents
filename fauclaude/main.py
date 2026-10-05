#!/usr/bin/env python3
"""fauclaude: Claude Code on the NHR@FAU "LLMs as a Service" gateway, with your
skills loaded for that session only.

1. **Auth and model wiring.** Resolve the personal `sk-...` gateway key, point
   Claude Code at `https://hub.nhr.fau.de/api/llmgw` (Anthropic shape, no
   `/v1`: Claude Code appends its own), and pin the small/haiku model so
   background calls stay on the gateway too. `ANTHROPIC_API_KEY` is blanked:
   setting it next to `ANTHROPIC_AUTH_TOKEN` makes Claude Code warn and can
   send auth down the API-key branch.

2. **Session-only skills.** Skills found under the profile's skill roots are
   staged into a generated plugin and handed over with `--plugin-dir`, which
   Claude Code scopes to the one session. A plain `claude` does not see them.

3. **Its own config dir.** `/model` and `/effort` write to the user settings,
   so picking a gateway model inside a session would leak into every ordinary
   `claude` launch. `CLAUDE_CONFIG_DIR` points these sessions at
   `~/.claude-fau/`, seeded once from the real config (theme, statusline,
   onboarding state). Global skills, MCP servers and session history are
   therefore not shared.

4. **Optional capture.** With `--capture` a local proxy sits between Claude
   Code and the gateway and writes every exchange as JSONL under
   `~/.claude-fau/traces/`. Off until turned on; the choice is remembered.
   Without capture Claude Code talks to the gateway directly. See `proxy` and
   `capture`.

`--profile` names the skill set: `~/.config/<profile>/skills` is read and the
generated plugin carries that name. ww3claude is this launcher with
`--profile ww3claude`.
"""

import json
import sys

# --advertise must answer before any heavy import (installer 5 s timeout).
PARENT_METADATA = {
    "name": "fauclaude",
    "capability": "agent",
    "domain": "llm",
    "desktop_file": "fauclaude.desktop",
    "icon": "utilities-terminal",
    "desc": "Claude Code on the free NHR@FAU LLM gateway, with your skills "
            "loaded for that session only",
    "terminal": True,
    "args": [],
    "tags": ["CLI"],
    "alias": "fauclaude",
    # No skill_name: this is a launcher, not a skill. The skills it stages are
    # session-scoped by design.
}

if "--advertise" in sys.argv:
    print(json.dumps([PARENT_METADATA]))
    sys.exit(0)

import argparse
import os
import shutil
import signal
import subprocess
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
for _path in (str(SCRIPT_DIR), str(SCRIPT_DIR.parent)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import capture  # noqa: E402
import proxy  # noqa: E402
from fau_agents import gateway, install, skills  # noqa: E402

HOME = Path.home()
DEFAULT_PROFILE = "fauclaude"

# Outside the repo: session transcripts and traces do not belong in a checkout.
CONFIG_DIR = Path(os.path.expanduser(
    os.environ.get("CLAUDE_FAU_CONFIG_DIR", HOME / ".claude-fau")))
REAL_CONFIG_DIR = Path(os.path.expanduser(
    os.environ.get("CLAUDE_CONFIG_DIR", HOME / ".claude")))
# This launcher's own settings (the capture choice), next to Claude Code's.
SETTINGS_FILE = CONFIG_DIR / "fau-agents.json"
# Cosmetics and state worth inheriting so the first launch is not an onboarding
# wizard. Anything permission- or model-shaped is left out on purpose: keeping
# those separate is the reason for a second config dir. `autoMode` is absent
# because its environment block describes whichever repo configured it.
_SEED_SETTINGS_KEYS = ("theme", "statusLine", "autoCompactWindow",
                       "skipAutoPermissionPrompt", "promptSuggestionEnabled")
_SEED_STATE_KEYS = ("hasCompletedOnboarding", "lastOnboardingVersion",
                    "installMethod", "firstStartTime", "hasIdeOnboardingBeenShown")


# --- plugin ------------------------------------------------------------------

def plugin_dir(profile: str) -> Path:
    override = os.environ.get("CLAUDE_FAU_PLUGIN_DIR")
    if override:
        return Path(os.path.expanduser(override))
    return skills.staged_dir("claude", profile)


def plugin_name(profile: str) -> str:
    return os.environ.get("CLAUDE_FAU_PLUGIN_NAME", profile)


def build_plugin(profile: str = DEFAULT_PROFILE, extra=(), dest: Path = None,
                 root=None) -> tuple:
    """Rebuild the session plugin from source. Returns (dest, [skill names])."""
    dest = dest or plugin_dir(profile)
    found = skills.discover(skills.skill_roots(profile, extra, root))
    if dest.exists():
        shutil.rmtree(dest)  # rebuilt wholesale: a removed skill must vanish
    (dest / ".claude-plugin").mkdir(parents=True)
    (dest / ".claude-plugin" / "plugin.json").write_text(json.dumps({
        "name": plugin_name(profile),
        "description": f"Skills loaded only inside {profile} sessions.",
        "version": "0.1.0",
    }, indent=2) + "\n", encoding="utf-8")
    names = skills.stage(dest / "skills", found)
    return dest, names


# --- config dir --------------------------------------------------------------

def ensure_config_dir(dest: Path = None, source: Path = None) -> Path:
    """Create the isolated config dir on first use; leave it alone afterwards.

    Only the keys above are copied, once. After that the user's own /model,
    /effort and permission choices live there and survive relaunch.
    """
    dest = dest or CONFIG_DIR
    source = source or REAL_CONFIG_DIR
    settings = dest / "settings.json"
    if settings.exists():
        return dest
    dest.mkdir(parents=True, exist_ok=True)
    seeded = {}
    try:
        real = json.loads((source / "settings.json").read_text(encoding="utf-8"))
        seeded = {k: real[k] for k in _SEED_SETTINGS_KEYS if k in real}
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    settings.write_text(json.dumps(seeded, indent=2) + "\n", encoding="utf-8")

    # ~/.claude.json is a sibling of the config dir, not inside it; the
    # onboarding flags live there, so seed the copy Claude Code looks for.
    state = {}
    try:
        real_state = json.loads((source.parent / ".claude.json").read_text(encoding="utf-8"))
        state = {k: real_state[k] for k in _SEED_STATE_KEYS if k in real_state}
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    state.setdefault("hasCompletedOnboarding", True)
    (dest / ".claude.json").write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    return dest


# --- capture setting ---------------------------------------------------------

def capture_enabled(path: Path = None) -> bool:
    """Off unless `--capture` turned it on at some earlier launch."""
    try:
        data = json.loads((path or SETTINGS_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(data, dict) and data.get("capture") is True


def set_capture(enabled: bool, path: Path = None) -> None:
    path = path or SETTINGS_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, json.JSONDecodeError):
        data = {}
    data["capture"] = enabled
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


# --- launch ------------------------------------------------------------------

# Claude Code stamps these onto every child process; inheriting them flags the
# new session as nested and turns off transcript saving.
_PARENT_SESSION_VARS = (
    "CLAUDECODE",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SSE_PORT",
    "CLAUDE_CODE_EXECPATH",
    "CLAUDE_PID",
)


def launch_env(key: str, base_url: str = gateway.BASE_URL, config_dir: Path = None) -> dict:
    env = os.environ.copy()
    for var in _PARENT_SESSION_VARS:
        env.pop(var, None)
    env["ANTHROPIC_BASE_URL"] = base_url
    env["ANTHROPIC_AUTH_TOKEN"] = key
    # Blank, not the key: both set makes Claude Code warn and may route auth
    # down the API-key branch. Empty counts as unset.
    env["ANTHROPIC_API_KEY"] = ""
    env["ANTHROPIC_SMALL_FAST_MODEL"] = gateway.DEFAULT_SMALL_MODEL
    env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = gateway.DEFAULT_SMALL_MODEL
    if config_dir is not None:
        env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    return env


def build_argv(model: str, plugin: Path, passthrough: list) -> list:
    return ["claude", "--model", model, "--plugin-dir", str(plugin), *passthrough]


def launch(model: str, passthrough: list, *, profile: str = DEFAULT_PROFILE,
           extra=(), dry_run: bool = False, skip_models: bool = False,
           isolated: bool = True, record: bool = False) -> int:
    key = gateway.resolve_key()
    if not key:
        print(gateway.missing_key_message(profile), file=sys.stderr)
        return 1
    plugin, names = build_plugin(profile, extra)
    config_dir = ensure_config_dir() if isolated else None
    print(f"skills (this session only): {', '.join(names) or 'none found'}", file=sys.stderr)
    if config_dir:
        print(f"config: {config_dir} (separate from ~/.claude)", file=sys.stderr)
    if not skip_models:
        gateway.print_models(key)

    base_url = gateway.BASE_URL
    server = None
    if record:
        recorder = capture.Recorder()
        server, base_url = proxy.serve(gateway.BASE_URL, key, recorder=recorder,
                                       context=gateway.ssl_context())
        print(f"  -> capturing exchanges to {recorder.dir} (proxy on {base_url})",
              file=sys.stderr)
    print(f"  -> starting Claude Code with: {model}", file=sys.stderr)
    print(file=sys.stderr)

    argv = build_argv(model, plugin, passthrough)
    if dry_run:
        print("[dry-run] would exec:", " ".join(argv))
        if server:
            server.shutdown()
        return 0
    env = launch_env(key, base_url, config_dir)
    if os.name == "nt":
        # On Windows the npm shim is claude.cmd, which CreateProcess does not
        # find under the bare name, and exec does not replace the process.
        exe = shutil.which("claude")
        if exe is None:
            print("claude is not on PATH", file=sys.stderr)
            if server:
                server.shutdown()
            return 1
        argv[0] = exe
    elif server is None:
        os.execvpe("claude", argv, env)  # replaces this process
    if server is None:
        return subprocess.call(argv, env=env)
    # With the proxy this process must outlive claude to keep serving it.
    # Ctrl-C belongs to the child, which has its own handler.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        return subprocess.call(argv, env=env)
    finally:
        server.shutdown()


# --- CLI ---------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="fauclaude",
        description="Claude Code on the NHR@FAU LLM gateway, with your skills "
                    "loaded for this session only. Unknown flags and anything "
                    "after -- go to claude.")
    ap.add_argument("--install", action="store_true", help="register the alias")
    ap.add_argument("--remove", action="store_true", help="unregister the alias")
    ap.add_argument("--model", default=gateway.DEFAULT_MODEL,
                    help=f"gateway model to run (default: {gateway.DEFAULT_MODEL})")
    ap.add_argument("--profile", default=DEFAULT_PROFILE,
                    help="skill set: reads ~/.config/<profile>/skills "
                         f"(default: {DEFAULT_PROFILE})")
    ap.add_argument("--skill-root", action="append", default=[], metavar="DIR",
                    help="also stage the skills under DIR (repeatable)")
    ap.add_argument("--list-skills", action="store_true",
                    help="show which skills would be staged, then exit")
    ap.add_argument("--sync", action="store_true",
                    help="rebuild the plugin directory, then exit")
    ap.add_argument("--no-models", action="store_true",
                    help="skip the gateway model listing")
    ap.add_argument("--shared-config", action="store_true",
                    help="use the global ~/.claude config instead of the "
                         "isolated one (then /model writes your global default)")
    capture_group = ap.add_mutually_exclusive_group()
    capture_group.add_argument("--capture", dest="capture", action="store_true",
                               default=None,
                               help="record exchanges to "
                                    f"{capture.DEFAULT_DIR} from now on (remembered)")
    capture_group.add_argument("--no-capture", dest="capture", action="store_false",
                               help="stop recording exchanges (remembered)")
    ap.add_argument("--traces", action="store_true",
                    help="show what has been captured so far, then exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the claude invocation instead of running it")
    return ap


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    known, passthrough = build_parser().parse_known_args(argv)
    if "--" in passthrough:
        passthrough.remove("--")
    if known.install or known.remove:
        return install.install_or_remove(PARENT_METADATA, __file__, known.remove)
    if known.capture is not None:
        set_capture(known.capture)
        print(f"capture {'on' if known.capture else 'off'} (remembered in "
              f"{SETTINGS_FILE})", file=sys.stderr)
    if known.list_skills:
        roots = skills.skill_roots(known.profile, known.skill_root)
        skills.print_listing(roots, skills.discover(roots))
        return 0
    if known.traces:
        info = capture.stats()
        print(f"{info['dir']}")
        print(f"{info['records']} exchanges in {info['files']} day file(s), "
              f"{info['bytes'] / 1e6:.1f} MB")
        print(f"capture is {'on' if capture_enabled() else 'off'}")
        return 0
    if known.sync:
        dest, names = build_plugin(known.profile, known.skill_root)
        print(f"plugin rebuilt: {dest}")
        print(f"skills: {', '.join(names) or 'none found'}")
        return 0
    return launch(known.model, passthrough, profile=known.profile,
                  extra=known.skill_root, dry_run=known.dry_run,
                  skip_models=known.no_models,
                  isolated=not known.shared_config, record=capture_enabled())


if __name__ == "__main__":
    sys.exit(main())
