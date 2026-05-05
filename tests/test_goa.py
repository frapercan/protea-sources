"""Smoke tests for the GOA source plugin (F2A.6 of master plan v3).

Same shape as ``protea-backends/tests/test_esm.py``: assert plugin
discoverable + contract compliant. ``load`` is a contract-surface
stub during F2A.6 (real pipeline lives in PROTEA's
``LoadGOAAnnotationsOperation``); the test only pins the
``NotImplementedError`` so a future refactor that accidentally drops
the stub fails loudly.
"""

from __future__ import annotations

from importlib.metadata import entry_points

import pytest
from protea_contracts import AnnotationSource

from protea_sources.goa import GoaSource, plugin


def test_plugin_is_goa_source_instance() -> None:
    assert isinstance(plugin, GoaSource)


def test_plugin_implements_annotation_source_abc() -> None:
    assert isinstance(plugin, AnnotationSource)


def test_plugin_name_is_goa() -> None:
    assert plugin.name == "goa"


def test_plugin_resolvable_via_entry_points() -> None:
    eps = entry_points(group="protea.sources")
    goa_eps = [ep for ep in eps if ep.name == "goa"]
    assert len(goa_eps) == 1
    resolved = goa_eps[0].load()
    assert resolved is plugin


def test_load_raises_not_implemented_during_f2a6() -> None:
    with pytest.raises(NotImplementedError, match="contract-surface stub"):
        plugin.load(session=None, payload={}, emit=lambda *a, **k: None)
