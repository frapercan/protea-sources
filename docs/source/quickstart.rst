Quickstart
==========

Install
-------

.. code-block:: bash

   pip install protea-sources

There are no per-source extras: the upstream protocols (HTTP, gzip,
TSV, FASTA) are covered by ``requests``, the only non-contracts runtime
dependency.

The ``emit`` callback
---------------------

Every stream method takes a keyword-only ``emit`` callback with the
signature ``emit(event: str, payload, fields: dict, level: str)``. In
PROTEA it routes structured events into the job log. Standalone, pass a
no-op:

.. code-block:: python

   def emit(*args, **kwargs):
       return None

GOA (bulk GAF)
--------------

.. code-block:: python

   from protea_contracts import GoaStreamPayload
   from protea_sources.goa import plugin as goa

   payload = GoaStreamPayload(gaf_url="https://example.com/small.gaf.gz")
   for record in goa.stream(payload, emit=emit):
       print(record.accession, record.go_id, record.evidence_code)
       # P12345 GO:0000123 IDA

QuickGO (TSV with ECO mapping)
------------------------------

.. code-block:: python

   from protea_contracts import EcoMappingPayload, QuickGoStreamPayload
   from protea_sources.quickgo import plugin as quickgo

   eco = quickgo.fetch_eco_mapping(
       EcoMappingPayload(url="https://example.com/gaf-eco.txt"), emit=emit
   )
   for record in quickgo.stream(QuickGoStreamPayload(), emit=emit):
       evidence_code = eco.get(record.eco_id, record.eco_id)

UniProt (FASTA stream)
----------------------

.. code-block:: python

   from protea_contracts import UniProtFastaStreamPayload
   from protea_sources.uniprot import plugin as uniprot

   payload = UniProtFastaStreamPayload(search_criteria="reviewed:true")
   for record in uniprot.stream_fasta(payload, emit=emit):
       print(record.accession, record.length, record.sequence_hash)

InterProScan (local subprocess)
-------------------------------

.. code-block:: python

   from protea_sources.interpro import InterProRunPayload, plugin as interpro

   payload = InterProRunPayload(fasta_path="/data/proteins.fasta", timeout_seconds=3600)
   for hit in interpro.run(payload, emit=emit):
       print(hit.accession, hit.source_db, hit.start, hit.end, hit.ipr_version)

Records are frozen pydantic models from ``protea-contracts``: a typo
fails at construction time, dataflow is one-way, and schema drift is
impossible.

Offline parsing
---------------

The pure parser functions take an in-memory blob, so you can pin
behaviour without any network or binary:

.. code-block:: python

   from protea_sources.goa import parse_gaf_text
   from protea_sources.quickgo import parse_quickgo_tsv
   from protea_sources.uniprot import parse_fasta_text
   from protea_sources.interpro import parse_interproscan_tsv

   records = list(parse_gaf_text(gaf_blob))
