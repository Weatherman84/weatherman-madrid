from __future__ import annotations

import sys
from types import ModuleType

import pytest

from runtime_bootstrap import discard_stale_weatherman_modules


@pytest.mark.parametrize("old_version", ["9.4.1", "9.5.2"])
def test_stale_streamlit_modules_are_discarded(monkeypatch, old_version):
    old_package = ModuleType("weatherman")
    old_package.__version__ = old_version
    old_settings = ModuleType("weatherman.settings")
    old_settings.airports = lambda: {}
    monkeypatch.setitem(sys.modules, "weatherman", old_package)
    monkeypatch.setitem(sys.modules, "weatherman.settings", old_settings)

    assert discard_stale_weatherman_modules("10.7.1") is True
    assert "weatherman" not in sys.modules
    assert "weatherman.settings" not in sys.modules


def test_current_streamlit_modules_are_kept(monkeypatch):
    current_package = ModuleType("weatherman")
    current_package.__version__ = "10.7.1"
    monkeypatch.setitem(sys.modules, "weatherman", current_package)

    assert discard_stale_weatherman_modules("10.7.1") is False
    assert sys.modules["weatherman"] is current_package
