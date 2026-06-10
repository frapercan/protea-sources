The annotation-source contract
==============================

Every plugin in this package subclasses
:class:`protea_contracts.AnnotationSource`. Since the
``F2A.6-real`` migration (D-MIGR-06) the ABC is a **marker shape**: it
fixes the two identity attributes and leaves the streaming surface to
each source, because the upstream databases differ too much to share a
single signature (a bulk GAF download, a paginated REST cursor, and a
local subprocess have nothing in common at the I/O level).

Identity attributes
-------------------

``name`` (class attribute)
   Stable string identifier. ``protea-core`` dispatches to a plugin by
   this name through the ``protea.sources`` entry-points group. It must
   match the sub-module directory name.

``version`` (class attribute or property)
   Human-readable source version, stored in
   ``AnnotationSet.source_version`` so a prediction set traces back to
   the exact release that produced it. May be a property when the
   version is read from a downloaded header at runtime (InterProScan
   threads the release tag per record instead).

Streaming surface
-----------------

A source exposes one or more **modality methods**. Each is a generator
that yields frozen pydantic records and takes a keyword-only ``emit``
callback:

.. code-block:: python

   def stream(self, payload, *, emit) -> Iterator[Record]:
       ...

==============  =========================  ==============================
Plugin          Method(s)                  Record type
==============  =========================  ==============================
``goa``         ``stream``                 ``GoaAnnotationRecord``
``quickgo``     ``stream``                 ``QuickGoAnnotationRecord``
``quickgo``     ``fetch_eco_mapping``      ``dict[str, str]``
``uniprot``     ``stream_fasta``           ``UniProtProteinRecord``
``uniprot``     ``stream_metadata``        ``UniProtMetadataRecord``
``interpro``    ``run`` / ``parse_tsv``    ``InterProAnnotation``
==============  =========================  ==============================

The ``emit`` callback
~~~~~~~~~~~~~~~~~~~~~~

``emit(event, payload, fields, level)`` streams lifecycle events into
the PROTEA job log without coupling the plugin to a logger instance.
Plugin events follow ``source.<name>.*``; the consuming operation adds
``<operation_name>.*`` events on top. Both land in the same
``JobEvent`` log. Standalone callers pass a no-op.

Typed payloads in, frozen records out
-------------------------------------

Every method takes a frozen ``*StreamPayload`` / ``*RunPayload`` from
``protea-contracts`` (InterProScan keeps :class:`InterProRunPayload`
local for now). Construction-time validation rejects a typo
(``gaf_uri`` for ``gaf_url``, a zero timeout) before a single byte
flies. Yielded records are equally frozen and ``extra="forbid"``, so
drift between the parser and the persistence layer fails at the
boundary, not three layers down.

Persistence boundary
--------------------

A plugin **never** imports ``sqlalchemy`` or ``protea-core`` and never
opens a database session. It yields records; the consuming operation
owns the session, the per-page commits, GO-term resolution against the
active ``OntologySnapshot``, and deduplication. This keeps every plugin
unit-testable against canned bytes.

.. _temporal-cutoff:

Temporal-cutoff semantics
-------------------------

Annotation sources are **time-versioned**. GOA and QuickGO records
carry an ``annotation_date`` field (the date the annotation was
asserted upstream); UniProt and InterProScan provenance carry release
tags. PROTEA's reference pool is built under a hard rule:

   **Reference annotations must be no newer than the reference
   timepoint** ``t0``. A snapshot aligned to a given release uses only
   annotations dated at or before that release; anything newer is the
   evaluation horizon and would leak future knowledge into the model.

The plugins are deliberately **cutoff-agnostic**: they yield every
parsed record verbatim and expose ``annotation_date`` (and the source
release tags) so the consuming operation can apply the cutoff filter.
Keeping the filter in the operation, not the plugin, means one source
download can be replayed at several cutoffs without re-fetching, and
the temporal policy lives in one auditable place rather than scattered
across four parsers.
