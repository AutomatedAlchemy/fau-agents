# fau-agents

fauclaude (Claude Code) and fauopencode (OpenCode) on the NHR@FAU LLM gateway,
with skills staged for one session. Public repo `AutomatedAlchemy/fau-agents`,
created 2026-10-05 from `MatSci/NHR/fauclaude` (fresh history). README.md has the
user-facing behaviour; this file covers the layout and the rules for changing it.

## Layout

- `fau_agents/` is shared code, imported by both launchers after they put the
  repo root on `sys.path`:
  - `gateway.py`: URLs, default models, key lookup, the `/v1/models` listing,
    and `ssl_context()` with the gateway's root CA (a fresh Windows lacks it).
  - `skills.py`: skill roots per profile, discovery, `{{CLI}}` rendering, staging
    into `.staged/<agent>/<profile>/`.
  - `install.py`: `--install`/`--remove` through cli-tools-kit, plus the pointer
    file `~/.config/fau-agents/checkout` that ww3-agents uses to find this checkout.
- `fauclaude/main.py`: launcher, config isolation, capture setting. `proxy.py`
  is the local proxy that runs only while capture is on, `capture.py` the trace
  writer inside it.
- `fauopencode/main.py`: builds the OpenCode config and execs `opencode`.
- `tests/`: stdlib unittest. `_paths.load(tool)` imports a launcher's `main.py`
  under a unique module name, since both are called `main.py`.

## Rules

- `--advertise` answers before any import beyond `json`/`sys` (installer timeout).
  The launchers declare no `skill_name`: their skills are session-scoped, a
  `skill_name` would make the installer register them globally.
- `requirements.txt` stays comment-only. The launchers are stdlib-only; only
  `--install` needs cli-tools-kit, which the installer running it already has.
- A profile is a skill-set name. It picks `~/.config/<profile>/skills`, the
  plugin name and the staged dir. ww3-agents (`FAU-WW3/ww3-agents`, private)
  calls these launchers with `--profile ww3claude --skill-root <tree>`; keep that
  interface stable or change both repos together.
- Capture is off by default and remembered in `~/.claude-fau/fau-agents.json`.
  Never turn it on from code.
- The key never goes into the OpenCode config text, only into the child's
  environment (`{env:LLMAPI_KEY}` in the config).

## Commits

Public GitHub repo outside the Gitea zone: commit when asked, push only on
instruction. Author is the GitHub noreply address.
