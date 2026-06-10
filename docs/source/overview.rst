Overview
========

``protea-sources`` is the **network and parsing** layer of annotation
ingestion in the PROTEA stack. Each sub-module owns one upstream
database, downloads a release, streams it, and yields typed pydantic
records from ``protea-contracts``. Persistence (DB filtering, GO-term
resolution, bulk insert) stays in the ``protea-core`` operations that
consume those streams.

Place in the stack
------------------

.. code-block:: text

   External databases
     └─ protea-sources        (download, parse, yield typed records)
           └─ protea-core operations   (filter, bulk insert, commit)
                 └─ PROTEA job queue

The plugin / operation boundary is a typed record stream defined in
``protea-contracts.records``: plugins yield frozen pydantic models and
know nothing about the ORM; operations own the session, the per-page
commits, and the deduplication logic.

Why a separate package
----------------------

1. **Plugin extensibility.** A new source is a single sub-module here
   plus one line in ``pyproject.toml``. ``protea-core`` is never
   touched: it resolves sources at runtime through
   ``importlib.metadata.entry_points``.
2. **Self-contained.** HTTP, parsing, and source-specific retries live
   here. The only non-contracts runtime dependency is ``requests``.
3. **Testable in isolation.** No database, no SQLAlchemy. Parser tests
   run against canned bytes; HTTP tests run against ``requests`` mocks.
   CI needs neither network nor a database.

Discovery
---------

``protea-core`` resolves a source by name through the
``protea.sources`` entry-points group:

.. code-block:: python

   from importlib.metadata import entry_points

   plugin = entry_points(group="protea.sources")["goa"].load()
   for record in plugin.stream(payload, emit=emit):
       ...

Supported sources
-----------------

.. list-table::
   :header-rows: 1
   :widths: 16 30 28 26

   * - Plugin
     - Upstream
     - Modality
     - Stream method
   * - :doc:`goa <sources/goa>`
     - UniProt-GOA bulk GAF (EBI FTP)
     - GAF 2.x over HTTP + gzip
     - ``stream``
   * - :doc:`quickgo <sources/quickgo>`
     - QuickGO REST API (EBI)
     - TSV, cursor pagination
     - ``stream`` + ``fetch_eco_mapping``
   * - :doc:`uniprot <sources/uniprot>`
     - UniProt REST API
     - FASTA + metadata TSV
     - ``stream_fasta`` + ``stream_metadata``
   * - :doc:`interpro <sources/interpro>`
     - InterProScan (local subprocess)
     - TSV per domain hit
     - ``run`` + ``parse_tsv``

Event model
-----------

Every plugin call takes an ``emit(event, payload, fields, level)``
callback so lifecycle progress streams into the PROTEA job log without
coupling the plugin to a logger instance. Plugin events follow the
``source.<name>.*`` convention; the consuming operation layers its own
``<operation_name>.*`` events on top. Both end up in the same
``JobEvent`` log.
