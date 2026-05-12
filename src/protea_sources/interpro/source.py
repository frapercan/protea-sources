"""InterProScan ``AnnotationSource`` implementation.

IP.1a keeps the runtime surface deliberately small: the plugin is a
marker for entry-point discovery and a thin wrapper around the TSV
parser. IP.1b will add ``run(...)`` (subprocess invocation of
``interproscan.sh``) and a ``stream`` method that pairs the runner
with the parser.

Why the plugin class exists at all in IP.1a:

* ``protea-core`` discovers sources by iterating the
  ``protea.sources`` entry-points group and isinstance-checking each
  ``.load()`` result against :class:`AnnotationSource`. Without a
  concrete subclass with ``name`` / ``version`` class attributes,
  the plugin would not show up in the registry.
* Future ``run`` / ``stream`` methods belong on the same class so the
  IP.1b PR is a method-add, not a class-rename.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

from protea_contracts import AnnotationSource

from protea_sources.interpro.parser import (
    InterProAnnotation,
    parse_interproscan_tsv,
)


class InterProSource(AnnotationSource):
    """InterProScan domain-annotation source.

    ``name``/``version`` follow the convention set by
    :class:`GoaSource` and :class:`QuickGoSource`: a stable string id
    for ``protea-core`` dispatch and a human-readable version tag.
    The release-specific version (e.g. ``"InterProScan-5.66-98.0"``)
    is carried per-record in :attr:`InterProAnnotation.ipr_release_version`,
    not at the class level, because a single PROTEA deployment can
    re-index against multiple InterProScan releases over time.
    """

    name = "interpro"
    version = "interproscan-tsv"

    def parse_tsv(
        self,
        source: str | os.PathLike[str],
    ) -> Iterator[InterProAnnotation]:
        """Adapter shim around :func:`parse_interproscan_tsv`.

        Accepts a filesystem path or an in-memory TSV blob. Exists on
        the plugin class so ``protea-core`` callers can reach the
        parser through the discovered plugin instance without
        importing the parser module directly — keeps the import
        surface narrow as IP.1b/IP.2 add more methods.
        """
        yield from parse_interproscan_tsv(source)
