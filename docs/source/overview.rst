Concepts
========

``protea-sources`` is the **annotation-ingestion layer** of the PROTEA
stack: the package that knows how to talk to each upstream database,
download a release, parse it, and hand back typed records. Everything
downstream (filtering, GO-term resolution, bulk insert) is somebody
else's job. This page covers the four ideas a reader needs before
touching any single plugin: the place in the stack, the
``AnnotationSource`` contract, entry-point discovery, and the
temporal-cutoff rule.

Place in the stack
------------------

.. code-block:: text

   External databases
     └─ protea-sources            (download, parse, yield typed records)
           └─ protea-core operations   (filter, bulk insert, commit)
                 └─ PROTEA job queue

The boundary between the two top layers is a **typed record stream**.
A plugin yields frozen pydantic models from ``protea-contracts`` and
knows nothing about the ORM. The consuming operation owns the database
session, the per-page commits, and the deduplication. That single line
of separation is what keeps every plugin testable without a database.

The ``AnnotationSource`` contract
---------------------------------

Every plugin subclasses :class:`protea_contracts.AnnotationSource`.
Since the ``F2A.6-real`` migration (D-MIGR-06) the ABC is a **marker
shape**: it fixes two identity attributes and leaves the streaming
surface to each source, because the upstream databases differ too much
to share one signature. A bulk GAF download, a paginated REST cursor,
and a local subprocess have nothing in common at the I/O level, so
forcing a single ``stream(payload)`` method would only paper over the
difference.

Identity attributes
~~~~~~~~~~~~~~~~~~~~~

``name`` (class attribute)
   Stable string identifier. ``protea-core`` dispatches to a plugin by
   this name through the ``protea.sources`` entry-points group, and it
   must match the sub-module directory name.

``version`` (class attribute or property)
   Human-readable source version, stamped onto
   ``AnnotationSet.source_version`` so a prediction set traces back to
   the exact release that produced it. It may be a property when the
   version is read from a downloaded header at runtime. InterProScan
   threads the release tag per record instead of pinning it on the
   class, because one deployment can re-index against several
   InterProScan releases over time.

The fetch / parse / emit model
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A source exposes one or more **modality methods**. Each is a generator
that fetches over the wire (or a subprocess), parses, and yields frozen
records, taking a keyword-only ``emit`` callback:

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

Each modality also ships a **pure parser** (``parse_gaf_text``,
``parse_quickgo_tsv``, ``parse_fasta_text``, ``parse_interproscan_tsv``)
that takes an in-memory blob and does no I/O. The streaming method is
the production path; the pure parser is what the unit tests pin against
canned bytes, so parser behaviour is fixed without a network or a
binary.

Typed payloads in, frozen records out
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Every method takes a frozen ``*StreamPayload`` / ``*RunPayload`` from
``protea-contracts`` (InterProScan keeps :class:`InterProRunPayload`
local for now). Construction-time validation rejects a typo
(``gaf_uri`` for ``gaf_url``, a zero timeout) before a single byte
flies. Yielded records are equally frozen and ``extra="forbid"``, so
drift between the parser and the persistence layer fails at the
boundary, not three layers down.

The ``emit`` callback
~~~~~~~~~~~~~~~~~~~~~~

``emit(event, payload, fields, level)`` streams lifecycle events into
the PROTEA job log without coupling the plugin to a logger instance.
Plugin events follow the ``source.<name>.*`` convention; the consuming
operation layers its own ``<operation_name>.*`` events on top, and both
land in the same ``JobEvent`` log. A standalone caller passes a no-op.

The persistence boundary
~~~~~~~~~~~~~~~~~~~~~~~~~~

A plugin **never** imports ``sqlalchemy`` or ``protea-core`` and never
opens a database session. It yields records; the consuming operation
owns the session, the per-page commits, GO-term resolution against the
active ``OntologySnapshot``, and deduplication. The only non-contracts
runtime dependency a plugin pulls in is ``requests``.

Discovery
---------

``protea-core`` never imports a plugin module directly. It resolves a
source by name at runtime through the ``protea.sources`` entry-points
group:

.. code-block:: python

   from importlib.metadata import entry_points

   plugin = entry_points(group="protea.sources")["goa"].load()
   for record in plugin.stream(payload, emit=emit):
       ...

The four entry points (``goa``, ``quickgo``, ``uniprot``, ``interpro``)
are declared in ``pyproject.toml`` under
``[tool.poetry.plugins."protea.sources"]``. Adding a source is a new
sub-module here plus one line there; ``protea-core`` picks it up at
startup with no code change. That extensibility is the whole reason the
sources live in their own package.

.. _temporal-cutoff:

Temporal-cutoff semantics
-------------------------

Annotation sources are **time-versioned**, and getting the time axis
right is what keeps PROTEA's evaluation leakage-free. GOA and QuickGO
records carry an ``annotation_date`` field (the date the annotation was
asserted upstream); UniProt and InterProScan provenance carries release
tags instead. The reference pool is built under a hard rule:

   **Reference annotations must be no newer than the reference
   timepoint** ``t0``. A snapshot aligned to a given release uses only
   annotations dated at or before that release; anything newer is the
   evaluation horizon and would leak future knowledge into the model.

The plugins are deliberately **cutoff-agnostic**: they yield every
parsed record verbatim and expose ``annotation_date`` (and the source
release tags) so the consuming operation applies the cutoff filter.
Keeping the filter in the operation, not the plugin, has two payoffs.
One source download can be replayed at several cutoffs without
re-fetching, and the temporal policy lives in one auditable place
rather than scattered across four parsers.

Each source page restates how its records expose the time axis, because
the mechanism differs (a per-row date for GOA and QuickGO, a release
tag for UniProt and InterProScan).
