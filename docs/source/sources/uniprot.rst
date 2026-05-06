UniProt (``uniprot``)
=====================

The ``uniprot`` plugin consumes the UniProt REST API for two
distinct workloads: streaming protein sequences (FASTA) and
fetching functional metadata (TSV).

:Source: UniProt REST API (FASTA + TSV, cursor pagination).
:Records: :class:`protea_contracts.UniProtProteinRecord`
          (FASTA stream),
          :class:`protea_contracts.UniProtMetadataRecord`
          (TSV stream, F2A.6-real step 4 in progress).
:Streaming entry points: ``UniProtSource.stream_fasta``,
                         ``UniProtSource.fetch_metadata``.

Operational notes
-----------------

- **Private retry client**. Network handling lives in
  ``_http.py``: exponential backoff with jitter, ``Retry-After``
  header parsing, and ``Link`` header cursor extraction. Centralised
  here so any future UniProt workload (e.g. xrefs) reuses the same
  retry policy.
- **FASTA header parsing**. The plugin parses ``OS=`` (organism),
  ``OX=`` (NCBI taxon ID), ``GN=`` (gene name) and ``PE=`` (existence
  evidence level) markers from the FASTA header line. Isoform
  accessions of the form ``P12345-2`` are recognised; the
  ``canonical_accession`` field collapses them back to ``P12345``.
- **Sequence hash**. Each yielded record carries a precomputed
  ``sequence_hash`` (MD5) so the consuming operation can deduplicate
  before bulk insert. The helper lives in
  ``protea_contracts.bio_utils`` (introduced in D-MIGR-04, turn 29).
- **Cursor pagination**. UniProt returns the next cursor in the
  ``Link`` HTTP header; ``_http.py`` extracts it via regex.
- **Coverage**: 100 % on ``uniprot/__init__``, 93 % on
  ``uniprot/_http``.
- **F2A.6-real step 4**: the ``fetch_metadata`` half is in progress;
  the FASTA half landed in turn 32 of master plan v3.

API reference
-------------

.. automodule:: protea_sources.uniprot
   :members:
   :show-inheritance:
   :member-order: bysource
