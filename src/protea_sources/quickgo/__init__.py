"""QuickGO REST API source.

Implements :class:`protea_contracts.AnnotationSource` for the EBI
QuickGO bulk-download endpoint. The plugin owns HTTP + TSV parsing
+ optional ECO mapping fetch; persistence (DB filtering, GO-term
resolution, bulk insert) stays in PROTEA's
``LoadQuickGOAnnotationsOperation`` which consumes the record stream.

The QuickGO TSV columns used (header names verbatim, the parser
maps them to record fields)::

    GENE PRODUCT ID  → record.accession
    GO TERM          → record.go_id
    QUALIFIER        → record.qualifier
    ECO ID           → record.eco_id  (raw; operation maps to evidence_code)
    REFERENCE        → record.db_reference
    WITH/FROM        → record.with_from
    ASSIGNED BY      → record.assigned_by
    DATE             → record.annotation_date

Two API divergences from GoaSource:

* **Per-batch HTTP**: when ``gene_product_ids`` is non-empty, the
  plugin batches them into ``gene_product_batch_size``-sized URL
  parameters to dodge QuickGO's ~8 KB URL-length limit (returns 400
  on overlong queries). When ``None``, a single unbatched request
  fetches the full universe.
* **Auxiliary fetch (D-MIGR-05)**: ``fetch_eco_mapping`` is a
  separate small-file fetch the operation calls once before
  iterating ``stream()``. The result (``dict[str, str]``) is cached
  in the operation for the duration of the load.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from typing import Any

import requests
from protea_contracts import (
    AnnotationSource,
    EcoMappingPayload,
    QuickGoAnnotationRecord,
    QuickGoStreamPayload,
)


def _open_tsv_text_stream(raw_stream: Any) -> io.TextIOWrapper:
    """Wrap a binary stream body in a UTF-8 text iterator.

    ``raw_stream`` is typed ``Any`` because urllib3 ``HTTPResponse``
    satisfies the ``read`` protocol expected by ``io.TextIOWrapper``
    but isn't statically declared as ``IO[bytes]``.
    """
    return io.TextIOWrapper(raw_stream, encoding="utf-8", errors="replace")


def parse_quickgo_row(
    row: dict[str, str],
) -> QuickGoAnnotationRecord | None:
    """Map a parsed TSV row dict to a :class:`QuickGoAnnotationRecord`.

    Returns ``None`` when the required fields ``GENE PRODUCT ID`` and
    ``GO TERM`` are absent or blank — those rows would be skipped by
    the operation anyway, so we filter at the plugin boundary to keep
    the iterator clean. Optional fields default to ``None`` (empty
    cells become ``None``, not ``""``).
    """
    accession = row.get("GENE PRODUCT ID", "").strip()
    go_id = row.get("GO TERM", "").strip()
    if not accession or not go_id:
        return None
    return QuickGoAnnotationRecord(
        accession=accession,
        go_id=go_id,
        qualifier=row.get("QUALIFIER", "").strip() or None,
        eco_id=row.get("ECO ID", "").strip() or None,
        db_reference=row.get("REFERENCE", "").strip() or None,
        with_from=row.get("WITH/FROM", "").strip() or None,
        assigned_by=row.get("ASSIGNED BY", "").strip() or None,
        annotation_date=row.get("DATE", "").strip() or None,
    )


def parse_quickgo_tsv(text: str) -> Iterator[QuickGoAnnotationRecord]:
    """Parse a QuickGO TSV string into a record iterator.

    The first non-empty line is the header (column names verbatim
    from QuickGO). Subsequent lines are zipped against it; rows with
    fewer columns than the header are skipped silently. Useful for
    offline tests; production paths go through
    :meth:`QuickGoSource.stream`.
    """
    header: list[str] | None = None
    for raw in text.splitlines():
        line = raw.rstrip("\n")
        if not line:
            continue
        parts = line.split("\t")
        if header is None:
            header = parts
            continue
        if len(parts) < len(header):
            continue
        record = parse_quickgo_row(dict(zip(header, parts, strict=False)))
        if record is not None:
            yield record


def parse_eco_mapping(text: str) -> dict[str, str]:
    """Parse a GAF ECO-mapping file into ``{ECO:XXXXXXX: CODE}``.

    Format is space-separated: ``ECO:0000314 IDA``. Lines that don't
    start with ``ECO:`` (comments, blanks) are skipped. Lines with
    fewer than 2 whitespace-separated tokens are skipped silently.
    """
    mapping: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[0].startswith("ECO:"):
            mapping[parts[0]] = parts[1]
    return mapping


class QuickGoSource(AnnotationSource):
    """QuickGO bulk-download source (TSV, paginated)."""

    name = "quickgo"
    version = "quickgo-rest"

    def stream(
        self,
        payload: QuickGoStreamPayload,
        *,
        emit: Any,
    ) -> Iterator[QuickGoAnnotationRecord]:
        """Yield :class:`QuickGoAnnotationRecord` instances.

        When ``payload.gene_product_ids`` is non-empty, requests are
        batched at ``gene_product_batch_size`` to avoid 400-on-long-URL
        responses. Otherwise a single unbatched request fetches all
        annotations for ``geneProductType=protein``.
        """
        ids = payload.gene_product_ids
        if not ids:
            yield from self._fetch_page(payload, gp_ids=None, batch_index=0, total=1, emit=emit)
            return

        batches = [
            ids[i : i + payload.gene_product_batch_size]
            for i in range(0, len(ids), payload.gene_product_batch_size)
        ]
        total = len(batches)
        emit(
            "source.quickgo.batching",
            None,
            {
                "total_accessions": len(ids),
                "total_batches": total,
                "batch_size": payload.gene_product_batch_size,
            },
            "info",
        )
        for batch_index, batch in enumerate(batches):
            yield from self._fetch_page(
                payload, gp_ids=batch, batch_index=batch_index, total=total, emit=emit
            )

    def _fetch_page(
        self,
        payload: QuickGoStreamPayload,
        *,
        gp_ids: list[str] | None,
        batch_index: int,
        total: int,
        emit: Any,
    ) -> Iterator[QuickGoAnnotationRecord]:
        params: dict[str, Any] = {"geneProductType": "protein"}
        if gp_ids:
            params["geneProductId"] = ",".join(gp_ids)

        headers = {
            "Accept": "text/tsv",
            "User-Agent": "PROTEA/load_quickgo_annotations",
        }
        emit(
            "source.quickgo.download_start",
            None,
            {
                "batch": batch_index + 1,
                "of": total,
                "accessions_in_batch": len(gp_ids) if gp_ids else "all",
                "_progress_current": batch_index + 1,
                "_progress_total": total,
            },
            "info",
        )

        resp = requests.get(
            payload.quickgo_base_url,
            params=params,
            headers=headers,
            stream=True,
            timeout=payload.timeout_seconds,
        )
        resp.raise_for_status()
        resp.raw.decode_content = True
        text_stream = _open_tsv_text_stream(resp.raw)

        header: list[str] | None = None
        with text_stream:
            for raw in text_stream:
                line = raw.rstrip("\n")
                if not line:
                    continue
                parts = line.split("\t")
                if header is None:
                    header = parts
                    continue
                if len(parts) < len(header):
                    continue
                record = parse_quickgo_row(dict(zip(header, parts, strict=False)))
                if record is not None:
                    yield record

    def fetch_eco_mapping(
        self,
        payload: EcoMappingPayload,
        *,
        emit: Any,
    ) -> dict[str, str]:
        """Auxiliary fetch (D-MIGR-05): download the GAF ECO mapping.

        Single-shot HTTP. Returns ``{ECO:XXXXXXX: CODE}``. The operation
        calls this once before iterating ``stream()`` and caches the
        result for the duration of the load.
        """
        emit("source.quickgo.eco_mapping_start", None, {"url": payload.url}, "info")
        resp = requests.get(payload.url, timeout=payload.timeout_seconds)
        resp.raise_for_status()
        mapping = parse_eco_mapping(resp.text)
        emit(
            "source.quickgo.eco_mapping_done",
            None,
            {"entries": len(mapping)},
            "info",
        )
        return mapping


#: Module-level plugin instance discovered via the
#: ``protea.sources`` entry_points group.
plugin = QuickGoSource()
