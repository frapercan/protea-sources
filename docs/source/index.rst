protea-sources
==============

Annotation source plugins for the PROTEA stack. Each sub-module
implements the :class:`protea_contracts.AnnotationSource` ABC and
registers via the ``protea.sources`` ``entry_points`` group.

A source plugin is responsible for the network and parsing side of
ingestion: downloading the upstream release (GAF, TSV, FASTA, etc.),
streaming records, and yielding typed pydantic objects from
``protea-contracts``. Persistence (DB filtering, GO-term resolution,
bulk insert) stays in ``protea-core`` operations
(``LoadGOAAnnotationsOperation``, ``LoadQuickGOAnnotationsOperation``,
``InsertProteinsOperation``, ``FetchUniProtMetadataOperation``) which
consume the record streams.

At a glance
-----------

.. list-table::
   :header-rows: 1
   :widths: 14 28 28 30

   * - Plugin
     - Source
     - Status
     - Notes
   * - :doc:`goa <sources/goa>`
     - UniProt-GOA bulk GAF
     - Real (F2A.6-real pre-25)
     - HTTP + gzip + GAF 2.x parser; 100 % coverage.
   * - :doc:`quickgo <sources/quickgo>`
     - QuickGO REST API
     - Real (F2A.6-real turn 27)
     - Cursor-based TSV streaming + ECO mapping; 100 % coverage.
   * - :doc:`uniprot <sources/uniprot>`
     - UniProt REST API
     - FASTA stream real (turn 32); metadata pending
     - Cursor-based FASTA + isoform parsing + private retry/backoff
       client (``_http.py``).

Install
-------

.. code-block:: bash

   pip install protea-sources

The package has no per-source extras: the upstream protocols
(HTTP + gzip / TSV / FASTA) are covered by ``requests`` alone, which
is a hard runtime dependency.

Discovery
---------

``protea-core`` resolves a source by name through
``importlib.metadata.entry_points``::

    from importlib.metadata import entry_points
    plugin = entry_points(group="protea.sources")["goa"].load()

Operational role
----------------

Each plugin exposes a streaming method that yields typed records.
The platform operation that consumes the stream (e.g.
``LoadGOAAnnotationsOperation``) handles per-page commits, filtering
against canonical accessions already in the database, GO-term
resolution against the active ``OntologySnapshot``, and structured
event emission. Plugin events follow the ``source.<name>.*`` naming
convention; operation events keep their existing names.

Contents
--------

.. toctree::
   :maxdepth: 2

   sources/index
   contributing
