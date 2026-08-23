from __future__ import annotations

import importlib
import sys


def discard_stale_weatherman_modules(expected_version: str) -> bool:
    """Remove modules retained by a Streamlit rerun after an in-place upgrade."""
    loaded_package = sys.modules.get("weatherman")
    if loaded_package is None:
        return False
    if getattr(loaded_package, "__version__", None) == expected_version:
        return False

    for module_name in tuple(sys.modules):
        if module_name == "weatherman" or module_name.startswith("weatherman."):
            sys.modules.pop(module_name, None)
    importlib.invalidate_caches()
    return True
