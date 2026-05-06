Adding a new annotation source
===============================

Adding a source plugin is a one-file commit in this repository plus
one line in ``pyproject.toml``. The platform learns about the new
source automatically through the ``protea.sources`` ``entry_points``
group.

Five steps
----------

1. **Create a sub-module** under
   ``src/protea_sources/<your_name>/__init__.py``. The directory name
   is the canonical plugin name and must match the ``name`` class
   attribute below.

2. **Implement the contract.** Subclass
   :class:`protea_contracts.AnnotationSource` and provide ``name``,
   ``version`` and the streaming methods your source needs:

   .. code-block:: python

      from typing import Any
      from collections.abc import Iterator
      import requests
      from protea_contracts import AnnotationSource, GoaAnnotationRecord

      class MySource(AnnotationSource):
          name = "mysource"
          version = "release-2026-01"

          def stream(
              self, payload: dict[str, Any], *, emit: Any
          ) -> Iterator[GoaAnnotationRecord]:
              # Open the upstream stream, parse, yield records.
              ...

      plugin = MySource()

3. **Register the entry point.** In ``pyproject.toml`` add::

      [tool.poetry.plugins."protea.sources"]
      mysource = "protea_sources.mysource:plugin"

4. **Add tests** under ``tests/test_mysource.py`` covering: instance
   type, ABC compliance, ``name`` attribute, discoverability via
   ``entry_points(group="protea.sources")``, and parser correctness
   on a small fixture (a few representative records). The existing
   ``test_goa.py`` / ``test_quickgo.py`` / ``test_uniprot.py`` are
   templates.

5. **Add a docs page** under
   ``docs/source/sources/<your_name>.rst`` following the structure of
   the existing ones (Source, Records, Streaming entry points,
   Operational notes, ``automodule``). The
   ``docs/source/sources/index.rst`` ``toctree`` picks the page up
   automatically once committed.

Conventions
-----------

- **Plugin emits source-level events.** Naming convention is
  ``source.<name>.<event>``. The consuming ``protea-core`` operation
  layers its own ``<operation_name>.<event>`` events on top; both
  event streams end up in the same ``JobEvent`` log.
- **No persistence in plugins.** A plugin yields records but never
  touches the database. The consuming operation owns the ORM session,
  the per-page commits, and the deduplication.
- **Network errors stay in ``_http.py``** (or equivalent private
  helper). The exception types raised at the public surface should
  not leak ``requests``-level details.
- **Records are typed** via ``protea-contracts``. If a source needs a
  new record shape, add it to ``protea-contracts`` first (a SemVer
  minor in ``protea-contracts``), then consume it here.

CI expectations
---------------

The ``protea-sources`` repository CI runs ``ruff``, ``mypy`` strict
and ``pytest`` with coverage thresholds at 99 % package-wide.
Per-plugin coverage is expected at 100 %; private helpers
(``_http.py``-style modules) are allowed to drop to ~90 % when
exception branches are hard to exercise without a real network.

Documentation
-------------

The Sphinx docs build is opt-in:

.. code-block:: bash

   poetry install --with docs
   cd docs && make html

Default ``poetry install`` does not pull Sphinx, keeping CI fast.
