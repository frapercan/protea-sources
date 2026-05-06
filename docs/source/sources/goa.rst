GOA (``goa``)
=============

The ``goa`` plugin streams UniProt-GOA bulk releases (e.g.
``goa_uniprot_all.gaf.gz``) hosted at EBI.

:Source: UniProt-GOA bulk GAF (HTTP, gzip).
:Records: :class:`protea_contracts.GoaAnnotationRecord`.
:Streaming entry point: ``GoaSource.stream``.

Operational notes
-----------------

- **GAF 2.x layout**. The plugin parses the 15-column GAF specification.
  Lines beginning with ``!`` are skipped (comments). Lines with fewer
  than 15 columns are malformed and skipped silently; the consuming
  operation reports counts via structured events.
- **No filtering at the plugin level**. Every parsed record is yielded;
  the operation downstream filters against canonical accessions
  present in the database before bulk insert.
- **gzip decoded on the fly** to avoid materialising the entire file
  in memory.
- **Coverage**: 100 % (``protea-sources`` package CI).

API reference
-------------

.. automodule:: protea_sources.goa
   :members:
   :show-inheritance:
   :member-order: bysource
