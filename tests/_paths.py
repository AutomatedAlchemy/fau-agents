"""Import paths for the tests: the repo root (for fau_agents), fauclaude/ (for
capture and proxy), and a loader for the two launchers, which are both
called main.py and so cannot both be imported as `main`."""

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
for _path in (REPO, REPO / "fauclaude"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))


def load(tool: str):
    """fauclaude/main.py as module `fauclaude_main`, and so on."""
    name = f"{tool}_main"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, REPO / tool / "main.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]
