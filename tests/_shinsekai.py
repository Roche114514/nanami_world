"""Load the world under test with the SDK from a Shinsekai installation."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

_parent = Path(__file__).resolve().parents[1]
WORLD_CODE = _parent if (_parent / "plugin.json").is_file() else _parent / "nanami_world"
PROJECT = Path(os.environ.get("SHINSEKAI_TEST_ROOT") or WORLD_CODE.parents[1]).expanduser().resolve()

if not (PROJECT / "sdk" / "register.py").is_file():
    raise RuntimeError("Set SHINSEKAI_TEST_ROOT to a Shinsekai installation containing sdk/register.py.")

sys.path.insert(0, str(PROJECT))

if WORLD_CODE != (PROJECT / "plugins" / "nanami_world").resolve():
    # A separate checkout must exercise its own source, not the installed plugin.
    import plugins

    _spec = importlib.util.spec_from_file_location(
        "plugins.nanami_world", WORLD_CODE / "__init__.py", submodule_search_locations=[str(WORLD_CODE)],
    )
    if _spec is None or _spec.loader is None:
        raise RuntimeError(f"Could not load the nanami_world checkout at {WORLD_CODE}.")
    _module = importlib.util.module_from_spec(_spec)
    sys.modules[_spec.name] = _module
    plugins.nanami_world = _module
    _spec.loader.exec_module(_module)
