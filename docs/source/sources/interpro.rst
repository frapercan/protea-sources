InterPro (``interpro``)
=======================

The ``interpro`` plugin runs InterProScan domain annotation via a local
subprocess call and parses the resulting TSV output. It is the source
used by PROTEA's ``run_interproscan_batch`` operation (IP.3) for
per-protein functional domain annotation.

:Source: InterProScan TSV output (local subprocess or pre-existing file).
:Records: :class:`protea_sources.interpro.parser.InterProAnnotation`.
:Streaming entry points: ``InterProSource.run``, :func:`protea_sources.interpro.parser.parse_interproscan_tsv`.

Operational notes
~~~~~~~~~~~~~~~~~

**Two invocation modes.**
The plugin supports offline parsing of pre-existing TSV files via
:func:`protea_sources.interpro.parser.parse_interproscan_tsv` (file path
or in-memory string blob), and live subprocess invocation via
:meth:`protea_sources.interpro.source.InterProSource.run`, which calls
``interproscan.sh`` with ``check=True`` and a configurable timeout.

**Binary resolution.**
The InterProScan binary is resolved in priority order: the
``binary_path`` field on :class:`protea_sources.interpro.payload.InterProRunPayload`,
then the ``PROTEA_INTERPROSCAN_BIN`` environment variable, and finally the
bare name ``interproscan.sh`` resolved via ``$PATH``. A missing binary
or a non-zero exit raises immediately so the caller's retry policy can
surface the failure cleanly.

**Release version.**
The plugin reads the optional ``#`` comment header emitted by
InterProScan (e.g. ``# InterProScan-5.66-98.0``) and threads the parsed
version string through every yielded record as ``ipr_release_version``.
When the comment header is absent the caller must supply the version
string explicitly via the payload.

**TSV column layout (0-based).**
InterProScan TSV output is parsed at these column offsets::

    col 0  : Protein accession    -> record.accession
    col 3  : Analysis (source DB) -> record.source_db
    col 6  : Start location       -> record.start
    col 7  : End location         -> record.end
    col 8  : Score (e-value)      -> record.evidence
    col 11 : InterPro accession   -> record.ipr_version

Columns 12 onwards (GO annotations, pathways) are optional and only
present when InterProScan is invoked with ``-iprlookup`` / ``-goterms``
/ ``-pa``. Rows without an InterPro mapping carry empty strings or
literal ``"-"`` tokens which the parser normalises to ``None``.

**Coverage**: 100 % on ``interpro/parser``, ``interpro/payload`` and
``interpro/source``; 90 %+ on exception branches.

**Out of scope for current slices:**

- IP.1c: REST-API fallback when the binary is absent.
- IP.2: ORM model ``interpro_annotation`` in PROTEA core.
- IP.3: ``run_interproscan_batch`` operation wiring this into the job queue.

Temporal cutoff
~~~~~~~~~~~~~~~

InterProScan annotations carry no per-row date. Provenance travels via
``ipr_release_version`` (the ``InterProScan-5.x-xx.0`` tag), threaded
onto every record from the ``#`` comment header or the
``interproscan.sh --version`` probe, so a re-index against a newer
release stays distinguishable downstream. See the :ref:`temporal-cutoff
rule <temporal-cutoff>`.

API reference
~~~~~~~~~~~~~

The generated module reference (``interpro.parser``,
``interpro.payload``, ``interpro.source``) is in the :doc:`API reference
</reference/index>`.
