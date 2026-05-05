"""QuickGO REST API source.

Implements :class:`protea_contracts.AnnotationSource` for the EBI
QuickGO bulk download endpoint (paginated TSV with optional
ECO→evidence-code mapping). The plugin is the contract surface; the
streaming + parsing + ``ProteinGOAnnotation`` insert pipeline lives
in PROTEA's ``LoadQuickGOAnnotationsOperation`` and stays there
until F2C of master plan v3 hoists the platform ORM into a package
both PROTEA and protea-sources can depend on.
"""

from __future__ import annotations

from typing import Any

from protea_contracts import AnnotationSource


class QuickGoSource(AnnotationSource):
    """QuickGO bulk download source (TSV, paginated)."""

    name = "quickgo"
    version = "quickgo-rest"

    def load(
        self,
        session: Any,
        payload: dict[str, Any],
        *,
        emit: Any,
    ) -> dict[str, Any]:
        """Run the QuickGO load pipeline.

        Contract-surface stub: PROTEA's ``LoadQuickGOAnnotationsOperation``
        owns the active implementation until F2C of master plan v3
        breaks the platform ORM circular dependency.
        """
        raise NotImplementedError(
            "QuickGoSource.load is a contract-surface stub. The active "
            "implementation lives in PROTEA's LoadQuickGOAnnotationsOperation; "
            "migration is scheduled for F2C of master plan v3."
        )


#: Module-level plugin instance discovered via the
#: ``protea.sources`` entry_points group.
plugin = QuickGoSource()
