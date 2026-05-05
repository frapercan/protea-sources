"""Smoke tests for protea-sources bootstrap."""

from __future__ import annotations

from importlib.metadata import entry_points

import protea_sources
from protea_sources import goa, quickgo, uniprot


def test_version_is_string() -> None:
    assert isinstance(protea_sources.__version__, str)


def test_submodules_importable() -> None:
    assert hasattr(goa, "plugin")
    assert hasattr(quickgo, "plugin")
    assert hasattr(uniprot, "plugin")


def test_entry_points_registered() -> None:
    """protea.sources entry_points group must list all 3 sub-modules.

    Plugin objects themselves are still placeholders (None) until F2A.6;
    here we only assert that the entry_points discovery works so
    protea-core can rely on it.
    """
    eps = entry_points(group="protea.sources")
    names = {ep.name for ep in eps}
    assert "goa" in names
    assert "quickgo" in names
    assert "uniprot" in names


def test_no_platform_imports_leak() -> None:
    import sys

    forbidden = {"sqlalchemy", "fastapi", "protea_core"}
    leaked = forbidden & set(sys.modules)
    assert not leaked, f"Forbidden modules leaked: {leaked}"
