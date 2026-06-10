GO and evidence-code mapping
============================

GO terms and their evidence codes arrive in different shapes across the
upstream sources. This page collects how each source represents them
and how the consuming operation normalises the result.

GO terms
--------

GO identifiers travel verbatim as the ``go_id`` field
(``GO:0000123`` form) on :class:`protea_contracts.GoaAnnotationRecord`
and :class:`protea_contracts.QuickGoAnnotationRecord`. The plugins do
**not** resolve, validate, or expand them against the ontology. The
consuming operation resolves each ``go_id`` against the active
``OntologySnapshot`` and applies any ancestor propagation; an id absent
from the snapshot is filtered there, not in the plugin.

InterProScan is different: it emits a member-database hit plus the
``InterPro`` entry it integrates into (``ipr_version``, e.g.
``IPR000123``). GO terms only appear in the optional columns 14+ when
InterProScan is invoked with ``-goterms`` / ``-iprlookup``; that
GO-to-domain mapping is handled downstream, not by the TSV parser.

Evidence codes (ECO mapping)
----------------------------

GOA GAF rows carry a three-letter evidence code directly
(``record.evidence_code``, e.g. ``IDA``, ``IEA``). QuickGO instead
emits the raw ECO ontology id (``record.eco_id``, e.g.
``ECO:0000314``). To make the two sources comparable, the QuickGO
plugin offers a one-shot lookup:

.. code-block:: python

   from protea_contracts import EcoMappingPayload
   from protea_sources.quickgo import plugin as quickgo

   eco = quickgo.fetch_eco_mapping(
       EcoMappingPayload(url="https://example.com/gaf-eco.txt"), emit=emit
   )
   # {"ECO:0000314": "IDA", "ECO:0000501": "IEA", ...}

   for record in quickgo.stream(payload, emit=emit):
       evidence_code = eco.get(record.eco_id, record.eco_id)

The mapping file is the GAF ECO cross-reference table (space-separated
``ECO:XXXXXXX CODE`` lines); :func:`protea_sources.quickgo.parse_eco_mapping`
parses it. The consuming operation fetches the mapping once, caches it
for the duration of the load, and applies it as each record streams in.
An empty or missing URL short-circuits to an empty mapping, in which
case the raw ``eco_id`` is kept.

Why this lives at the operation layer
-------------------------------------

The plugin yields the raw upstream value (``eco_id`` or
``evidence_code``) and never rewrites it. Normalising ECO ids to
three-letter codes is a cross-source decision (it has to agree with how
GOA records are stored), so it belongs in the operation that owns both
streams, not inside one source parser.

Temporal cutoff
---------------

Evidence codes participate in the temporal-cutoff filter described in
the :ref:`temporal-cutoff rule <temporal-cutoff>`: the reference pool keeps
only annotations dated at or before the reference timepoint ``t0``, and
electronic (``IEA``) versus experimental evidence is a downstream
policy choice. The plugins expose ``annotation_date`` and the raw
evidence value so that policy can be applied without re-fetching.
