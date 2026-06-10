Sources
=======

One page per source plugin. Each describes what the source pulls, the
streaming method exposed, the record types yielded, the per-source
temporal-cutoff handling, and any source-specific quirks (cursor
extraction, ECO mapping, isoform parsing). A final page,
:doc:`go-mapping`, collects how GO terms and evidence codes are
represented across the four sources and normalised downstream.

.. toctree::
   :maxdepth: 1

   goa
   quickgo
   uniprot
   interpro
   go-mapping
