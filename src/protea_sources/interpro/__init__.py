"""InterProScan source plugin (IP.1a — scaffold + TSV adapter).

Implements :class:`protea_contracts.AnnotationSource` for the
InterProScan domain-annotation pipeline. This first iteration (IP.1a)
scopes the work to:

* A typed result schema (:class:`InterProAnnotation`) carrying the
  seven fields PROTEA persists per domain hit.
* A parser that ingests **InterProScan TSV output** (file path or
  in-memory blob).
* Entry-point registration under the ``protea.sources`` group so
  ``protea-core`` discovers the plugin at startup.

Delivered in IP.1b:

* :meth:`InterProSource.run` invokes ``interproscan.sh`` via
  :mod:`subprocess`, captures the stdout TSV, and yields parsed
  records with the ``--version`` release tag threaded through.
* :class:`InterProRunPayload` carries the typed inputs (FASTA path,
  extra CLI flags, timeout, optional binary override).
* ``PROTEA_INTERPROSCAN_BIN`` env variable selects a non-default
  binary path when ``payload.binary_path`` is unset.

Deferred to follow-up slices:

* IP.1c (optional): REST-API fallback.
* IP.2: ORM model ``interpro_annotation`` in PROTEA core.
* IP.3: ``run_interproscan_batch`` operation that wires the plugin
  into PROTEA's job queue.

InterProScan TSV column layout (1-indexed in the official spec,
0-indexed here so the offsets match the parser code)::

    col 1  (idx 0)  : Protein accession    -> record.accession
    col 4  (idx 3)  : Analysis (source DB) -> record.source_db
    col 7  (idx 6)  : Start location       -> record.start
    col 8  (idx 7)  : End location         -> record.end
    col 9  (idx 8)  : Score (e-value)      -> record.evidence
    col 12 (idx 11) : InterPro accession   -> record.ipr_version

The ``ipr_release_version`` field carries the InterProScan release
string (e.g. ``"InterProScan-5.66-98.0"``). It is parsed from the
optional ``#`` comment header line if present, otherwise supplied
explicitly by the caller (the IP.1b CLI runner will pass it from
``interproscan.sh --version``).

Spec reference:
https://interproscan-docs.readthedocs.io/en/latest/OutputFormats.html#tab-separated-values-tsv
"""

from __future__ import annotations

from protea_sources.interpro.interpro2go import (
    ENV_INTERPRO2GO_RELEASE,
    GO_ROOTS,
    INTERPRO2GO_RELEASE,
    InterProGOPrediction,
    load_obo_ancestors,
    propagate_go_predictions,
)
from protea_sources.interpro.parser import (
    InterProAnnotation,
    extract_go_terms,
    parse_interproscan_tsv,
    parse_interproscan_tsv_line,
    parse_release_version_header,
)
from protea_sources.interpro.payload import InterProRunPayload
from protea_sources.interpro.source import ENV_BINARY_PATH, InterProSource

#: Module-level plugin instance discovered via the ``protea.sources``
#: entry_points group. ``pyproject.toml`` registers it as
#: ``interpro = "protea_sources.interpro:plugin"``.
plugin = InterProSource()

__all__ = [
    "ENV_BINARY_PATH",
    "ENV_INTERPRO2GO_RELEASE",
    "GO_ROOTS",
    "INTERPRO2GO_RELEASE",
    "InterProAnnotation",
    "InterProGOPrediction",
    "InterProRunPayload",
    "InterProSource",
    "extract_go_terms",
    "load_obo_ancestors",
    "parse_interproscan_tsv",
    "parse_interproscan_tsv_line",
    "parse_release_version_header",
    "plugin",
    "propagate_go_predictions",
]
