"""Smoke tests for the UniProt source plugin (F2A.6 of master plan v3)."""

from __future__ import annotations

from importlib.metadata import entry_points

import pytest
from protea_contracts import AnnotationSource

from protea_sources.uniprot import UniProtSource, plugin


def test_plugin_is_uniprot_source_instance() -> None:
    assert isinstance(plugin, UniProtSource)


def test_plugin_implements_annotation_source_abc() -> None:
    assert isinstance(plugin, AnnotationSource)


def test_plugin_name_is_uniprot() -> None:
    assert plugin.name == "uniprot"


def test_plugin_resolvable_via_entry_points() -> None:
    eps = entry_points(group="protea.sources")
    up_eps = [ep for ep in eps if ep.name == "uniprot"]
    assert len(up_eps) == 1
    resolved = up_eps[0].load()
    assert resolved is plugin


def test_load_raises_not_implemented_during_f2a6() -> None:
    with pytest.raises(NotImplementedError, match="contract-surface stub"):
        plugin.load(session=None, payload={}, emit=lambda *a, **k: None)
