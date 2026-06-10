protea-sources
==============

``protea-sources`` is the annotation-ingestion layer of the PROTEA
stack. It holds the plugins that talk to the upstream biological
databases (UniProt-GOA, QuickGO, UniProt, InterProScan), download a
release, parse it, and yield typed records. Persistence (filtering,
GO-term resolution, bulk insert) stays in the ``protea-core``
operations that consume those record streams; a plugin never opens a
database session.

The problem it solves
---------------------

PROTEA ingests annotations from several sources that agree on almost
nothing: a multi-million-line GAF dump over HTTP, a cursor-paginated
REST API, a FASTA stream, and a local subprocess that shells out to
``interproscan.sh``. Two things have to stay constant across all of
them. First, the platform should learn about a new source without any
code change, so sources are **plugins discovered via entry points**.
Second, evaluation has to be leakage-free, so every source has to
expose its records with **consistent temporal semantics** (a date or a
release tag the downstream cutoff can act on). ``protea-sources`` is
the package that makes both true.

What lives here
---------------

.. list-table::
   :header-rows: 1
   :widths: 14 28 28 30

   * - Plugin
     - Source
     - Modality
     - Notes
   * - :doc:`goa <sources/goa>`
     - UniProt-GOA bulk GAF (EBI)
     - GAF 2.x over HTTP + gzip
     - Streaming GAF parser; ``annotation_date`` per row.
   * - :doc:`quickgo <sources/quickgo>`
     - QuickGO REST API (EBI)
     - TSV, cursor pagination
     - Batched queries + optional ECO evidence-code mapping.
   * - :doc:`uniprot <sources/uniprot>`
     - UniProt REST API
     - FASTA + metadata TSV
     - Cursor pagination + private retry/backoff client.
   * - :doc:`interpro <sources/interpro>`
     - InterProScan (local subprocess)
     - TSV per domain hit
     - Subprocess runner + pure TSV parser; per-record release tag.

Every plugin subclasses :class:`protea_contracts.AnnotationSource`,
registers under the ``protea.sources`` entry-points group, and yields
frozen pydantic records from ``protea-contracts``. The
:doc:`overview` page explains that contract and the temporal-cutoff
rule; the :doc:`quickstart` runs a minimal load from each source.

Reading order
-------------

.. toctree::
   :maxdepth: 2

   overview
   quickstart
   sources/index
   reference/index
   contributing
