"""UniProt REST source (FASTA + metadata).

Implements :class:`protea_contracts.AnnotationSource` for the
UniProt REST API: paginated FASTA via ``insert_proteins`` semantics
(cursor-based, exponential backoff + jitter) and TSV metadata via
``fetch_uniprot_metadata``. The plugin is the contract surface; the
underlying pipelines live in PROTEA's ``InsertProteinsOperation`` /
``FetchUniProtMetadataOperation`` and stay there until F2C of master
plan v3 hoists the platform ORM out of PROTEA.

Note: UniProt is technically a protein / sequence source rather than
a pure GO-annotation source, but it slots into ``AnnotationSource``
because it produces the canonical-accession universe and metadata
rows that the GOA / QuickGO pipelines filter against.
"""

from __future__ import annotations

from typing import Any

from protea_contracts import AnnotationSource


class UniProtSource(AnnotationSource):
    """UniProt REST source (FASTA + metadata)."""

    name = "uniprot"
    version = "uniprot-rest"

    def load(
        self,
        session: Any,
        payload: dict[str, Any],
        *,
        emit: Any,
    ) -> dict[str, Any]:
        """Run the UniProt load pipeline.

        Contract-surface stub: PROTEA's ``InsertProteinsOperation`` and
        ``FetchUniProtMetadataOperation`` own the active implementations
        until F2C of master plan v3 breaks the platform ORM circular
        dependency.
        """
        raise NotImplementedError(
            "UniProtSource.load is a contract-surface stub. The active "
            "implementations live in PROTEA's InsertProteinsOperation + "
            "FetchUniProtMetadataOperation; migration is scheduled for F2C "
            "of master plan v3."
        )


#: Module-level plugin instance discovered via the
#: ``protea.sources`` entry_points group.
plugin = UniProtSource()
