"""Smoke tests for the QuickGO source plugin (F2A.6 of master plan v3)."""

from __future__ import annotations

from importlib.metadata import entry_points

import pytest
from protea_contracts import AnnotationSource

from protea_sources.quickgo import QuickGoSource, plugin


def test_plugin_is_quickgo_source_instance() -> None:
    assert isinstance(plugin, QuickGoSource)


def test_plugin_implements_annotation_source_abc() -> None:
    assert isinstance(plugin, AnnotationSource)


def test_plugin_name_is_quickgo() -> None:
    assert plugin.name == "quickgo"


def test_plugin_resolvable_via_entry_points() -> None:
    eps = entry_points(group="protea.sources")
    qg_eps = [ep for ep in eps if ep.name == "quickgo"]
    assert len(qg_eps) == 1
    resolved = qg_eps[0].load()
    assert resolved is plugin


def test_load_raises_not_implemented_during_f2a6() -> None:
    with pytest.raises(NotImplementedError, match="contract-surface stub"):
        plugin.load(session=None, payload={}, emit=lambda *a, **k: None)
