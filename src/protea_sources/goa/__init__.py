"""UniProt-GOA bulk download source.

Placeholder for F2A.6. Will register an ``AnnotationSource``
implementation via ``protea_sources.goa:plugin``.
"""

# Sentinel so the entry_point in pyproject.toml has something
# to resolve to during F0/F1. Real plugin object lands in F2A.6.
plugin: object | None = None
