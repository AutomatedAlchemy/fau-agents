"""--install / --remove through cli-tools-kit, plus the checkout pointer.

The pointer file lets a wrapper in another repo (ww3claude, ww3opencode) find
this checkout when it is not a sibling of the wrapper's own repo.
"""

from pathlib import Path

from fau_agents.skills import REPO_DIR, XDG_CONFIG

POINTER = XDG_CONFIG / "fau-agents" / "checkout"
_FIELDS = ("name", "desktop_file", "icon", "desc", "terminal", "args", "tags",
           "alias", "capability", "domain")


def write_pointer(path: Path = None) -> Path:
    path = path or POINTER
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(REPO_DIR) + "\n", encoding="utf-8")
    return path


def install_or_remove(metadata: dict, script_path: str, remove: bool) -> int:
    try:
        from cli_tools_kit import ToolInstaller, ToolMetadata
    except ImportError:
        print("--install needs cli-tools-kit: pip install 'cli-tools-kit>=1.0,<2'")
        return 1
    meta = ToolMetadata(**{k: metadata[k] for k in _FIELDS if k in metadata})
    installer = ToolInstaller(script_path=script_path, metadata=meta)
    if remove:
        # The pointer stays: the other launcher in this repo may still use it.
        installer.remove()
    else:
        installer.install()
        write_pointer()
    return 0
