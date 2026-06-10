QuickGO (``quickgo``)
=====================

The ``quickgo`` plugin consumes the QuickGO bulk download REST API
hosted by EBI. Used by PROTEA when a finer-grained query (filtered
by taxon or evidence code, or scoped to a sub-set of GO terms) is
needed compared to the full GOA dump.

:Source: QuickGO REST API (TSV, cursor pagination).
:Records: :class:`protea_contracts.QuickGoAnnotationRecord`,
          :class:`protea_contracts.EcoMappingPayload`.
:Streaming entry points: ``QuickGoSource.stream``,
                         ``QuickGoSource.fetch_eco_mapping``.

Operational notes
-----------------

- **Cursor pagination**. The QuickGO API returns the next page cursor
  in a custom header; the plugin extracts it and re-issues until
  exhausted. Page size is parameter on the payload.
- **ECO evidence-code mapping**. Optional lookup: when an ECO URL is
  provided in the payload, ``fetch_eco_mapping`` produces the
  ``eco_id -> three_letter`` dict that the consuming operation
  applies during ingestion. An empty URL short-circuits to an empty
  mapping.
- **Plugin-emitted events**. Naming convention
  ``source.quickgo.{batching,decoded,fetched_page,...}``. The
  consuming ``LoadQuickGOAnnotationsOperation`` emits its own
  ``load_quickgo_annotations.*`` events on top.
- **Coverage**: 100 % (``protea-sources`` package CI).

Temporal cutoff
---------------

Each record carries ``annotation_date`` (the QuickGO ``DATE`` column)
and a raw ``eco_id``. The plugin yields every annotation verbatim; the
consuming operation maps the ECO id to an evidence code (see
:doc:`go-mapping`) and keeps only annotations dated at or before the
reference timepoint ``t0``. See :ref:`the contract page
<temporal-cutoff>` for the full rule.

API reference
-------------

.. automodule:: protea_sources.quickgo
   :members:
   :show-inheritance:
   :member-order: bysource
