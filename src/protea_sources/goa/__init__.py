"""UniProt-GOA bulk download source.

Implements :class:`protea_contracts.AnnotationSource` for the
EBI-hosted UniProt-GOA GAF releases (e.g.
``goa_uniprot_all.gaf.gz``). The plugin handles HTTP + gzip + GAF
parsing; persistence (DB filtering, GO-term resolution, bulk insert)
stays in PROTEA's ``LoadGOAAnnotationsOperation`` which consumes the
record stream.

GAF 2.x column layout (1-indexed in spec, 0-indexed in code)::

    col 2  (idx 1)  : DB Object ID         → record.accession
    col 4  (idx 3)  : Qualifier            → record.qualifier
    col 5  (idx 4)  : GO ID                → record.go_id
    col 6  (idx 5)  : DB:Reference         → record.db_reference
    col 7  (idx 6)  : Evidence Code        → record.evidence_code
    col 8  (idx 7)  : With (or) From       → record.with_from
    col 14 (idx 13) : Date                 → record.annotation_date
    col 15 (idx 14) : Assigned by          → record.assigned_by

Lines starting with ``!`` are GAF comments and are skipped. Lines
with fewer than 15 columns are malformed and skipped silently — the
operation reports the discrepancy via line counts.
"""

from __future__ import annotations

import gzip
import io
from collections.abc import Iterator
from typing import Any

import requests
from protea_contracts import AnnotationSource, GoaAnnotationRecord, GoaStreamPayload

# GAF column indices (0-based). The names mirror the spec rows.
_IDX_ACCESSION = 1
_IDX_QUALIFIER = 3
_IDX_GO_ID = 4
_IDX_DB_REFERENCE = 5
_IDX_EVIDENCE = 6
_IDX_WITH_FROM = 7
_IDX_DATE = 13
_IDX_ASSIGNED_BY = 14

_MIN_COLUMNS = 15


def _open_gaf_text_stream(raw_stream: Any, compressed: bool) -> io.TextIOWrapper:
    """Wrap a binary stream body in a UTF-8 text iterator.

    GAF distributions are either ``.gaf`` plain text or ``.gaf.gz``;
    both are decoded to UTF-8 with replacement-character fallback so a
    single bad byte never aborts a multi-million-line stream.

    ``raw_stream`` is typed ``Any`` because both file-like objects and
    urllib3 ``HTTPResponse`` instances satisfy the ``read`` protocol
    expected by ``gzip.GzipFile`` and ``io.TextIOWrapper``, but neither
    is statically declared as ``IO[bytes]``.
    """
    if compressed:
        return io.TextIOWrapper(
            gzip.GzipFile(fileobj=raw_stream),
            encoding="utf-8",
            errors="replace",
        )
    return io.TextIOWrapper(raw_stream, encoding="utf-8", errors="replace")


def parse_gaf_line(line: str) -> GoaAnnotationRecord | None:
    """Parse a single GAF line into a record, or return ``None`` to skip.

    Splitting and column extraction live here so unit tests can pin the
    parser behaviour without spinning up an HTTP server. Returns
    ``None`` for empty lines, comment lines (``!`` prefix), and lines
    with fewer than 15 tab-separated columns.
    """
    if not line or line.startswith("!"):
        return None
    parts = line.split("\t")
    if len(parts) < _MIN_COLUMNS:
        return None
    return GoaAnnotationRecord(
        accession=parts[_IDX_ACCESSION],
        go_id=parts[_IDX_GO_ID],
        qualifier=parts[_IDX_QUALIFIER] or None,
        evidence_code=parts[_IDX_EVIDENCE] or None,
        db_reference=parts[_IDX_DB_REFERENCE] or None,
        with_from=parts[_IDX_WITH_FROM] or None,
        assigned_by=parts[_IDX_ASSIGNED_BY] or None,
        annotation_date=parts[_IDX_DATE] or None,
    )


def parse_gaf_text(text: str) -> Iterator[GoaAnnotationRecord]:
    """Parse an in-memory GAF text into a record iterator.

    Useful for offline tests and small-batch tooling. Production
    workflows go through :meth:`GoaSource.stream` which streams over
    an HTTP response without materialising the body.
    """
    for raw in text.splitlines():
        record = parse_gaf_line(raw.rstrip("\n"))
        if record is not None:
            yield record


class GoaSource(AnnotationSource):
    """UniProt-GOA GAF bulk download source."""

    name = "goa"
    version = "uniprot-goa"

    def stream(
        self,
        payload: GoaStreamPayload,
        *,
        emit: Any,
    ) -> Iterator[GoaAnnotationRecord]:
        """Yield :class:`GoaAnnotationRecord` instances parsed from a GAF URL.

        ``payload`` is a frozen :class:`GoaStreamPayload` carrying the
        ``gaf_url`` and ``timeout_seconds`` knobs. Construction-time
        validation catches typos (``gaf_uri`` instead of ``gaf_url``,
        zero timeout) before a single HTTP byte flies.

        Empty lines, comment lines (``!`` prefix), and malformed rows
        (fewer than 15 columns) are skipped silently. The caller owns
        any policy around per-page commits and accession filtering.
        """
        emit("source.goa.download_start", None, {"gaf_url": payload.gaf_url}, "info")
        resp = requests.get(payload.gaf_url, stream=True, timeout=payload.timeout_seconds)
        resp.raise_for_status()

        compressed = payload.gaf_url.endswith(".gz")
        raw_stream = resp.raw
        raw_stream.decode_content = True

        text_stream = _open_gaf_text_stream(raw_stream, compressed)
        with text_stream:
            for raw in text_stream:
                record = parse_gaf_line(raw.rstrip("\n"))
                if record is not None:
                    yield record

        emit("source.goa.download_done", None, {"gaf_url": payload.gaf_url}, "info")


#: Module-level plugin instance discovered via the
#: ``protea.sources`` entry_points group.
plugin = GoaSource()
