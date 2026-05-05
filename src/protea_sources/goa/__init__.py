"""UniProt-GOA bulk download source.

Implements :class:`protea_contracts.AnnotationSource` for the
EBI-hosted UniProt-GOA GAF releases (e.g.
``goa_uniprot_all.gaf.gz`` versioned by GOA release number).
The plugin is the contract surface; the actual streaming + parsing
+ ``ProteinGOAnnotation`` insert pipeline currently lives in
PROTEA's ``LoadGOAAnnotationsOperation`` and stays there until F2C
of master plan v3 hoists the platform ORM out of PROTEA.

Why ``load`` raises ``NotImplementedError`` for now: protea-sources
is a leaf package — it cannot import ``protea.infrastructure.orm``
without inverting the dependency direction the C-stack refactor is
fixing. Real plugin logic moves here once the ORM models live in a
package both PROTEA and protea-sources can depend on.
"""

from __future__ import annotations

from typing import Any

from protea_contracts import AnnotationSource


class GoaSource(AnnotationSource):
    """UniProt-GOA GAF bulk download source.

    ``version`` is set per-call by the load implementation (read from
    the GAF header). The class default is the family identifier; the
    actual release version is recorded in ``AnnotationSet.source_version``
    once the load completes.
    """

    name = "goa"
    version = "uniprot-goa"

    def load(
        self,
        session: Any,
        payload: dict[str, Any],
        *,
        emit: Any,
    ) -> dict[str, Any]:
        """Run the GOA load pipeline.

        Currently a contract-surface stub: the streaming + parsing +
        bulk-insert path lives in PROTEA's ``LoadGOAAnnotationsOperation``
        and stays there until the platform ORM extraction (F2C of
        master plan v3) eliminates the circular import.
        """
        raise NotImplementedError(
            "GoaSource.load is a contract-surface stub. The active "
            "implementation lives in PROTEA's LoadGOAAnnotationsOperation; "
            "migration is scheduled for F2C of master plan v3."
        )


#: Module-level plugin instance discovered via the
#: ``protea.sources`` entry_points group.
plugin = GoaSource()
