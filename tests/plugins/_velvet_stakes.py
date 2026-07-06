"""Shared loader for velvet-stakes plugin modules.

The plugin directory name contains a dash (``plugins/velvet-stakes``), so its
modules cannot be imported with a plain ``import``. This mirrors the
spec-loading approach used by ``test_disk_cleanup_plugin.py`` but registers
the package namespace *without* executing ``__init__.py`` so the pure engine
modules can be tested without pulling in gateway dependencies.
"""

import importlib.util
import sys
import types
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "velvet-stakes"
PKG = "hermes_plugins.velvet_stakes"


def _ensure_parent_namespace() -> None:
    if "hermes_plugins" not in sys.modules:
        ns = types.ModuleType("hermes_plugins")
        ns.__path__ = []
        sys.modules["hermes_plugins"] = ns


def _ensure_package_namespace() -> None:
    """Register the plugin package as a namespace WITHOUT running __init__.py."""
    _ensure_parent_namespace()
    if PKG not in sys.modules:
        pkg = types.ModuleType(PKG)
        pkg.__path__ = [str(PLUGIN_DIR)]
        pkg.__package__ = PKG
        sys.modules[PKG] = pkg


def load_module(name: str):
    """Import ``plugins/velvet-stakes/<name>.py`` as a package submodule."""
    _ensure_package_namespace()
    full = f"{PKG}.{name}"
    if full in sys.modules:
        return sys.modules[full]
    spec = importlib.util.spec_from_file_location(full, PLUGIN_DIR / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = PKG
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


def load_plugin_init():
    """Import the plugin ``__init__.py`` (the hook entry point)."""
    _ensure_parent_namespace()
    existing = sys.modules.get(PKG)
    if existing is not None and getattr(existing, "__file__", None):
        return existing
    spec = importlib.util.spec_from_file_location(
        PKG,
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = PKG
    mod.__path__ = [str(PLUGIN_DIR)]
    sys.modules[PKG] = mod
    spec.loader.exec_module(mod)
    return mod
