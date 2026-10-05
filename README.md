# fau-agents

Claude Code and OpenCode on the [NHR@FAU "LLMs as a Service"](https://hpc.fau.de/request-llm-api-key/)
gateway (free for FAU members, hosted on-premises), with your own skills loaded
for that session only.

- **fauclaude** runs [Claude Code](https://docs.claude.com/en/docs/claude-code) against the
  gateway's Anthropic-compatible endpoint.
- **fauopencode** runs [OpenCode](https://opencode.ai) against the gateway's
  OpenAI-compatible endpoint.

Both are stdlib-only Python launchers. They find your key, pick a gateway model,
stage your skills, and then hand over to `claude` or `opencode`. You install
those two yourself.

```
fauclaude                       # launch (deepseek-ai/DeepSeek-V4-Flash-0731 by default)
fauclaude --model <model>       # another hosted model
fauclaude --list-skills         # which skills would be staged, and from where
fauclaude --dry-run             # print the claude invocation instead of running it
fauclaude --capture             # record exchanges from now on (remembered)
fauclaude --no-capture          # stop recording (remembered)
fauclaude --traces              # how much has been recorded, and whether capture is on
fauclaude --shared-config       # use ~/.claude instead of the separate profile
fauclaude -- --resume           # anything after -- (or any unknown flag) goes to claude

fauopencode                     # launch the OpenCode TUI
fauopencode run "explain x.py"  # unknown arguments go to opencode
fauopencode --models            # list the gateway's chat models
fauopencode --dry-run           # print the generated config instead of running
```

## Install

Through the [AutomatedAlchemy installer](https://github.com/AutomatedAlchemy/alchemy-installer),
or by hand:

```
python3 fauclaude/main.py --install     # alias `fauclaude` in ~/.tools_aliases
python3 fauopencode/main.py --install   # alias `fauopencode`
```

`--install` needs [cli-tools-kit](https://pypi.org/project/cli-tools-kit/), which the
installer already has. `--remove` takes the alias out again.

## Key

Request a personal key at <https://hpc.fau.de/request-llm-api-key/>. The launchers
look in this order:

1. `LLMAPI_KEY` in the environment,
2. `~/.faullm_env`,
3. `$FAULLM_DIR/.env`,
4. `../NHR/tools/faullm/.env` next to this checkout (where the WW3 installer puts
   the faullm tool).

A file holds one line, `LLMAPI_KEY=sk-...`. Keep it at mode 600 and out of git.

## Session-only skills

A skill is a directory with a `SKILL.md` and an entry point. The launchers collect
every `SKILL.md` under the skill roots and stage copies into
`.staged/<agent>/<profile>/` inside this checkout (gitignored, rebuilt on every
launch). fauclaude passes the staged directory to Claude Code as `--plugin-dir`,
fauopencode lists it in OpenCode's `skills.paths`. Both scope it to the one
session, so a plain `claude` or `opencode` in the same terminal does not see these
skills.

`{{CLI}}` in a skill body becomes an absolute command for that tool, because a
shell alias does not expand in the non-interactive shells an agent runs commands
in. The entry point is `main.py`, `cli.py`, `__main__.py`, `<dir_name>.py` or
`<dir-name>.py`, run with the tool's own `.venv` when there is one. An executable
`main`, `cli`, `run.sh` or `<dir-name>` works too.

Roots, searched in order (on a name collision the first root wins):

1. `~/.config/<profile>/skills` and `~/.local/share/<profile>/skills`,
2. each `--skill-root DIR`,
3. the working directory, unless it is `$HOME`.

The profile is `fauclaude` unless `--profile` says otherwise. Fill
`~/.config/fauclaude/skills` with tool directories or symlinks to them; the
AutomatedAlchemy installer has a "fauclaude" skill target that does this for you.
`CLAUDE_FAU_SKILL_ROOT` replaces the whole list with a PATH-style list of trees.
Matching goes four directories deep plus `*/*/tools/*/`. Hidden directories,
`node_modules`, `vendor` and the like are skipped, so a project's own
`.claude/skills/` stays with the agent's native loading.

The WW3 group's `ww3-agents` (ww3claude, ww3opencode) runs these two launchers
with `--profile ww3claude` and the WW3 tool tree as a skill root.

## fauclaude

### Its own config dir

`/model` and `/effort` write to the global user settings. Picking a gateway model
inside a session would leave it in `~/.claude/settings.json` and break the next
plain `claude`. So fauclaude sessions run with `CLAUDE_CONFIG_DIR=~/.claude-fau`,
seeded once from the real config with cosmetics and onboarding state only, never
`model`, `effortLevel`, `permissions` or `autoMode`. After that the profile is
yours. Globally installed skills, MCP servers and session history live in
`~/.claude` and do not appear here. `--shared-config` opts out.

### Captured traces

Capture is off until you run `fauclaude --capture`. The choice is stored in
`~/.claude-fau/fau-agents.json` and holds until `--no-capture`. Without capture,
Claude Code talks to the gateway directly. With capture on, a proxy on 127.0.0.1
passes each exchange through unchanged and appends it to
`~/.claude-fau/traces/<date>.jsonl`:

```json
{"ts": "...", "model": "...", "stream": true,
 "request":  {"system": ..., "messages": [...], "tools": [...], "thinking": ...},
 "response": {"content": [{"type": "thinking", ...}, {"type": "text", ...},
                          {"type": "tool_use", "input": {...}}],
              "stop_reason": "tool_use", "usage": {...}},
 "meta": {"duration_s": 4.1, "dedup": "...", "prefix": "...", "turns": 7}}
```

The proxy is the only place where a complete prompt and completion pair exists.
Claude Code's transcripts hold the rendered conversation without the system prompt
or the tool schemas.

- Claude Code resends the whole history each turn, so consecutive records share
  a growing prefix. `meta.dedup` (a hash of the request) collapses retries.
  `meta.prefix` (a hash of everything before the last message) chains the turns
  of one conversation.
- Failed exchanges are kept, with `response: null` or a partial turn and
  `meta.error`.
- Auth headers and `metadata.user_id` are never written. Everything else the
  session saw is, including file contents and paths. Treat the directory as being
  as sensitive as the projects you use it on. Nothing is uploaded.

### Auth details

The session gets `ANTHROPIC_AUTH_TOKEN` and an explicitly blank
`ANTHROPIC_API_KEY`. Setting both makes Claude Code warn about ambiguous auth and
can send the request down the API-key branch. The small/haiku model is pinned to
`google/gemma-4-E4B-it` so background calls stay on the gateway.

The gateway's certificate chains to the HARICA TLS ECC Root CA 2021. A fresh
Windows does not have that root in its store, so the launchers bring it along for
their own calls (model listing, capture proxy). Claude Code and OpenCode carry
their own CA lists.

### The old stream repair

Until 2026-10-05 fauclaude always ran a proxy that repaired the gateway's stream.
Its Anthropic translation opened a block as `text` and sent `thinking_delta`s into
it, and Claude Code aborted the turn with `API Error: Content block is not a
thinking block` (8 of 10 turns on DeepSeek-V4-Flash at `xhigh`, 2026-08-18).
Rechecks found 0 of 25 direct turns broken on 2026-08-24 and 0 of 10 Claude Code
sessions on 2026-10-05, so the repair was removed. It is in
`fauclaude/sse_repair.py` at commit 84edab9 if the error comes back.

## fauopencode

fauopencode builds an OpenCode config and passes it in `OPENCODE_CONFIG_CONTENT`:
one provider `fau` (`@ai-sdk/openai-compatible`, base URL
`https://hub.nhr.fau.de/api/llmgw/v1`), the gateway's chat models with their
context sizes, `enabled_providers: ["fau"]`, `share: "disabled"`, and the staged
skills. The key goes in as `LLMAPI_KEY` in the environment; the config only
names it (`{env:LLMAPI_KEY}`).

- OpenCode also loads `~/.claude/skills` and `~/.agents/skills` by itself. Those
  global skills appear next to the staged ones.
- There is no capture. OpenCode talks to the gateway's OpenAI endpoint directly.
- The output limit per model is capped at 32768 tokens. The gateway reports the
  full context size as the output limit, which would leave no room for the prompt.

## Settings

| Variable | Default | Effect |
|---|---|---|
| `LLMAPI_KEY` | (see Key) | gateway key |
| `DEFAULT_CLAUDE_FAU_LLM` | `deepseek-ai/DeepSeek-V4-Flash-0731` | default model |
| `DEFAULT_CLAUDE_FAU_SMALL_LLM` | `google/gemma-4-E4B-it` | model for background calls |
| `CLAUDE_FAU_SKILL_ROOT` | | replaces the skill roots |
| `CLAUDE_FAU_SKILL_GLOBS` | | replaces the `SKILL.md` patterns |
| `CLAUDE_FAU_PYTHON` | this interpreter | interpreter for tools without a `.venv` |
| `CLAUDE_FAU_CONFIG_DIR` | `~/.claude-fau` | fauclaude's Claude Code profile |
| `CLAUDE_FAU_CAPTURE_DIR` | `~/.claude-fau/traces` | where traces go |
| `CLAUDE_FAU_PLUGIN_DIR`, `CLAUDE_FAU_PLUGIN_NAME` | | staged plugin location and name |

## Tests

```
python3 -m unittest discover -s tests
```

## License

MIT
