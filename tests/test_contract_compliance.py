"""Contract-compliance suite for the protea-sources plugin pack (F6.4).

Sibling slice T1.7 (PROTEA PR #371) shipped
``tests/test_contracts_invariants.py`` in the platform repo. F6.4 is the
plugin-side equivalent: this module enumerates every entry point under
the ``protea.sources`` group and asserts that the loaded object honours
the :class:`protea_contracts.AnnotationSource` contract. The check is
deliberately group-driven (not hard-coded plugin names) so a new source
added in ``pyproject.toml`` is automatically covered, and a regression
where someone forgets to subclass the ABC fails the suite without any
test edit.

The entry-point target for every plugin is a *singleton instance* of
the implementing class (see ``goa/__init__.py``: ``plugin = GoaSource()``).
The tests therefore use :func:`isinstance` rather than
:func:`issubclass`.
"""

from __future__ import annotations

import importlib
from importlib.metadata import entry_points
from pathlib import Path

import pytest
import tomllib
from protea_contracts import AnnotationSource

ENTRY_GROUP = "protea.sources"


def _discovered_entry_points() -> list:
    eps = list(entry_points(group=ENTRY_GROUP))
    if not eps:
        pytest.fail(
            f"No entry points found under group {ENTRY_GROUP!r}; "
            "protea-sources is not installed or pyproject.toml is misconfigured."
        )
    return eps


def test_every_entry_point_is_importable() -> None:
    """Each declared entry_point target must resolve without ImportError.

    Catches broken module paths (typo in ``pyproject.toml``) before they
    cascade into protea-core startup failures.
    """
    for ep in _discovered_entry_points():
        ep.load()


def test_every_entry_point_satisfies_annotation_source_abc() -> None:
    """Every loaded plugin must be an :class:`AnnotationSource` instance.

    Mirrors T1.7's PROTEA-side invariant: any plugin registered under
    the ``protea.sources`` group MUST honour the ABC marker contract so
    that ``protea-core`` can dispatch through ``isinstance`` checks.
    """
    for ep in _discovered_entry_points():
        plugin = ep.load()
        assert isinstance(plugin, AnnotationSource), (
            f"Entry point {ep.name!r} (target={ep.value!r}) loaded as {type(plugin)!r}, "
            "which is not a protea_contracts.AnnotationSource subclass."
        )


def test_every_plugin_declares_name_and_version() -> None:
    """Required class attributes on the ABC contract.

    ``protea-core`` indexes plugins by ``name`` and stamps
    ``AnnotationSet.source_version`` with ``version``; both must be
    non-empty strings.
    """
    for ep in _discovered_entry_points():
        plugin = ep.load()
        assert isinstance(plugin.name, str) and plugin.name, (
            f"{ep.name!r} has empty or non-string 'name' attribute."
        )
        assert isinstance(plugin.version, str) and plugin.version, (
            f"{ep.name!r} has empty or non-string 'version' attribute."
        )


def test_pyproject_pins_protea_contracts() -> None:
    """``protea-contracts`` must be declared as a runtime dependency.

    F6.4 acceptance: each plugin repo's CI runs contract tests against a
    pinned protea-contracts version. The pin here is the git-branch dep
    (the canonical shape across the plugin stack while the contracts
    package iterates on ``main``); a future bookkeeping commit promotes
    it to a semver tag, at which point this test continues to pass.
    """
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    deps = data["tool"]["poetry"]["dependencies"]
    assert "protea-contracts" in deps, (
        "pyproject.toml [tool.poetry.dependencies] is missing 'protea-contracts'."
    )


def test_protea_contracts_version_is_resolvable() -> None:
    """The installed ``protea-contracts`` must expose ``__version__``.

    A successful import + version probe proves the CI lane resolved a
    real wheel/sdist, not a phantom path-dep that silently no-ops.
    """
    contracts = importlib.import_module("protea_contracts")
    assert isinstance(contracts.__version__, str) and contracts.__version__, (
        "protea_contracts.__version__ is missing or empty; "
        "the runtime dependency did not resolve to a valid package."
    )
