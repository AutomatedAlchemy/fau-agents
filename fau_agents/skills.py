"""Find skills on disk and stage them for one session.

A skill is a directory holding a `SKILL.md` and an entry point. Each launcher
copies the skills it finds into a staging directory inside this checkout and
hands that directory to the agent for one session only, so a plain `claude` or
`opencode` never sees them.

`{{CLI}}` in a skill body becomes an absolute command for that tool: its own
venv interpreter plus the script path. A shell alias would not do, because an
agent runs commands in non-interactive shells where aliases do not expand.

The staging is rebuilt from the source `SKILL.md` files on every launch, so an
edited skill takes effect at the next start.
"""

import os
import shutil
import sys
from pathlib import Path

HOME = Path.home()
REPO_DIR = Path(__file__).resolve().parents[1]
XDG_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config"))

# Hidden, so a skill root that contains this checkout never picks up the
# staged copies again.
STAGED_DIR = REPO_DIR / ".staged"

# Bounded depths rather than one `**`: `**` walks .venv, node_modules and
# vendored source trees on every launch. Depth 1-3 covers a directory of linked
# tools and a tree of tool repos; `*/*/tools/*/` covers repos that keep their
# tools one level down (NHR/tools/faullm).
DEFAULT_GLOBS = (
    "SKILL.md", "*/SKILL.md", "*/*/SKILL.md", "*/*/*/SKILL.md",
    "*/*/tools/*/SKILL.md",
)
# Never descend into these, whatever the glob matched. Hidden directories are
# pruned too (pathlib's `*` matches them, unlike the shell): `.venv` is huge,
# `.staged` holds our own output, and a project's own `.claude/skills/` is
# already loaded by the agent itself.
PRUNE = {"node_modules", "site-packages", "dist-packages", "vendor", "target"}


def _globs() -> tuple:
    custom = os.environ.get("CLAUDE_FAU_SKILL_GLOBS", "")
    return tuple(g for g in custom.split(os.pathsep) if g) or DEFAULT_GLOBS


def profile_dirs(profile: str) -> list:
    """The per-user skill directories of one profile. An installer links the
    skills a user picked into the first one."""
    return [XDG_CONFIG / profile / "skills",
            HOME / ".local" / "share" / profile / "skills"]


def skill_roots(profile: str = "fauclaude", extra=(), root=None) -> list:
    """Roots to scan, existing and de-duplicated, in precedence order.

    `root` (a path or a list) is the whole answer when given. Otherwise
    `CLAUDE_FAU_SKILL_ROOT`, a PATH-style list, is. Otherwise: the profile's own
    directories, then `extra`, then the working directory unless that is $HOME
    (a depth-3 sweep of a home directory is slow and finds unrelated trees).
    """
    if root is not None:
        candidates = [Path(root)] if isinstance(root, (str, Path)) else [Path(r) for r in root]
    elif os.environ.get("CLAUDE_FAU_SKILL_ROOT"):
        candidates = [Path(os.path.expanduser(p)) for p
                      in os.environ["CLAUDE_FAU_SKILL_ROOT"].split(os.pathsep) if p]
    else:
        cwd = Path.cwd()
        candidates = [*profile_dirs(profile),
                      *(Path(os.path.expanduser(str(e))) for e in extra),
                      *([cwd] if cwd != HOME else [])]
    seen, roots = set(), []
    for cand in candidates:
        try:
            resolved = cand.resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.is_dir():
            continue
        seen.add(resolved)
        roots.append(resolved)
    return roots


def skill_name(body: str, fallback: str) -> str:
    """`name:` from the SKILL.md frontmatter; the directory name if absent."""
    for line in body.splitlines():
        if line.startswith("---"):
            continue
        if line.startswith("name:"):
            return line.split(":", 1)[1].strip()
        if line.strip() and ":" not in line:
            break
    return fallback


def interpreter_for(tool_dir: Path) -> str:
    """The tool's own venv if it has one (several tools only work from theirs),
    else `CLAUDE_FAU_PYTHON` when set, else the interpreter running us."""
    for venv in (".venv", "venv", "env"):
        for rel in (("bin", "python3"), ("Scripts", "python.exe")):
            candidate = tool_dir.joinpath(venv, *rel)
            if candidate.exists():
                return str(candidate)
    shared = os.environ.get("CLAUDE_FAU_PYTHON")
    if shared and Path(os.path.expanduser(shared)).exists():
        return os.path.expanduser(shared)
    return sys.executable


def entry_point(tool_dir: Path) -> str:
    """The command string that runs the tool."""
    # `<dirname>.py` with dashes as underscores is how a one-tool repo names
    # its script (manim-kit ships manim_kit.py).
    own = tool_dir.name.replace("-", "_") + ".py"
    for name in ("main.py", "cli.py", "__main__.py", own, f"{tool_dir.name}.py"):
        script = tool_dir / name
        if script.exists():
            return f'"{interpreter_for(tool_dir)}" "{script}"'
    for name in ("main", "cli", "run.sh", tool_dir.name):
        script = tool_dir / name
        if script.is_file() and os.access(script, os.X_OK):
            return str(script)
    # Nothing found: still render something absolute rather than a stale
    # `{{CLI}}`, so the failure is a clear "no such file".
    return f'"{interpreter_for(tool_dir)}" "{tool_dir / "main.py"}"'


def discover(roots) -> list:
    """[(skill name, source SKILL.md, rendered body)], sorted by name.
    The first root to define a name wins."""
    found, seen = [], set()
    for base in roots:
        for src in sorted(src for pattern in _globs() for src in base.glob(pattern)):
            tool_dir = src.parent
            parts = tool_dir.relative_to(base).parts
            if any(p in PRUNE or p.startswith(".") for p in parts):
                continue
            try:
                body = src.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            name = skill_name(body, tool_dir.name)
            if name in seen:
                continue
            seen.add(name)
            found.append((name, src, body.replace("{{CLI}}", entry_point(tool_dir))))
    return sorted(found, key=lambda item: item[0])


def staged_dir(agent: str, profile: str) -> Path:
    """Where one launcher stages one profile. Separate per agent and profile, so
    fauclaude and ww3claude running side by side do not rebuild each other's."""
    return STAGED_DIR / agent / profile


def stage(dest: Path, skills: list) -> list:
    """Write `<dest>/<name>/SKILL.md` for each skill, after clearing `dest` so a
    removed skill disappears. Returns the names."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for name, _src, body in skills:
        (dest / name).mkdir(parents=True, exist_ok=True)
        (dest / name / "SKILL.md").write_text(body, encoding="utf-8")
    return [name for name, _src, _body in skills]


def print_listing(roots: list, skills: list) -> None:
    """What --list-skills prints."""
    print("roots: " + (os.pathsep.join(str(r) for r in roots) or "(none found)"))
    for name, src, _body in skills:
        print(f"{name:20s} {src}")
    if not skills:
        print("no SKILL.md found. Link skills into the profile directory, pass "
              "--skill-root DIR, or set CLAUDE_FAU_SKILL_ROOT (PATH-style list).")
