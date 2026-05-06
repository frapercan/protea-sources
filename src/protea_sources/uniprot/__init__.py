"""UniProt REST source (FASTA + metadata).

Implements :class:`protea_contracts.AnnotationSource` for UniProt's
REST search endpoint. The plugin owns:

* HTTP retry/backoff with jitter (private :mod:`._http` helper).
* Cursor-based pagination via the ``Link: ...; rel="next"`` header.
* FASTA header parsing (``sp|<acc>|<entry_name> ... OS=... OX=... GN=...``).
* Isoform splitting and sequence hashing via
  :mod:`protea_contracts.bio_utils`.

Persistence (FK-safe upsert against ``Protein`` + ``Sequence`` tables)
stays in PROTEA's :class:`InsertProteinsOperation` which consumes the
record stream.

The plugin currently exposes:

* :meth:`UniProtSource.stream_fasta` — yields
  :class:`UniProtProteinRecord` instances over UniProt FASTA pages.
* :meth:`UniProtSource.fetch_metadata` is reserved for F2A.6-real
  step 4 (UniProt metadata migration); ``stream`` and ``load`` remain
  as deprecation shells until the ABC cleanup (D-MIGR-06).
"""

from __future__ import annotations

import gzip
import re
from collections.abc import Iterator
from io import BytesIO
from typing import Any
from urllib.parse import quote

from protea_contracts import (
    AnnotationSource,
    UniProtFastaStreamPayload,
    UniProtProteinRecord,
    compute_sequence_hash,
    parse_isoform,
)

from protea_sources.uniprot._http import UniProtRetryClient, extract_next_cursor

_RE_OS = re.compile(r"\bOS=([^=]+?)\sOX=")
_RE_OX = re.compile(r"\bOX=(\d+)")
_RE_GN = re.compile(r"\bGN=([^\s]+)")


def parse_fasta_header(header: str) -> dict[str, Any]:
    """Parse a UniProt FASTA header line into a dict of fields.

    Handles the canonical form ``sp|<accession>|<entry_name> <description>
    OS=<organism> OX=<taxon_id> GN=<gene>`` plus the unreviewed
    ``tr|<accession>|<entry_name>`` variant. Headers without the pipe
    structure fall back to taking the first whitespace-delimited token
    as the accession.

    The function is pure (no I/O, no DB) so it's testable against
    canned bytes; production callers go through :meth:`UniProtSource
    .stream_fasta`.
    """
    parts = header.split("|")
    reviewed = header.startswith("sp|")

    if len(parts) >= 3:
        accession = parts[1].strip()
        entry_name: str | None = parts[2].split(" ", 1)[0].strip()
    else:
        accession = header.split(" ", 1)[0].strip()
        entry_name = None

    canonical, is_canonical, iso_idx = parse_isoform(accession)

    organism: str | None = None
    taxonomy_id: str | None = None
    gene_name: str | None = None

    m = _RE_OS.search(header)
    if m:
        organism = m.group(1).strip()
    m = _RE_OX.search(header)
    if m:
        taxonomy_id = m.group(1).strip()
    m = _RE_GN.search(header)
    if m:
        gene_name = m.group(1).strip()

    return {
        "accession": accession,
        "entry_name": entry_name,
        "canonical_accession": canonical,
        "is_canonical": is_canonical,
        "isoform_index": iso_idx,
        "organism": organism,
        "taxonomy_id": taxonomy_id,
        "gene_name": gene_name,
        "reviewed": reviewed,
    }


def parse_fasta_text(fasta_text: str) -> Iterator[UniProtProteinRecord]:
    """Parse a UniProt FASTA string into a record iterator.

    Header lines start with ``>``; subsequent non-empty lines are
    sequence content. Whitespace inside the sequence is stripped.
    Records with empty sequences are skipped silently (UniProt
    occasionally returns malformed entries).
    """
    header: str | None = None
    seq_lines: list[str] = []

    def flush() -> UniProtProteinRecord | None:
        if not header:
            return None
        seq = "".join(seq_lines).replace(" ", "").strip()
        if not seq:
            return None
        parsed = parse_fasta_header(header)
        return UniProtProteinRecord(
            accession=parsed["accession"],
            entry_name=parsed["entry_name"],
            canonical_accession=parsed["canonical_accession"],
            is_canonical=parsed["is_canonical"],
            isoform_index=parsed["isoform_index"],
            organism=parsed["organism"],
            taxonomy_id=parsed["taxonomy_id"],
            gene_name=parsed["gene_name"],
            reviewed=parsed["reviewed"],
            sequence=seq,
            length=len(seq),
            sequence_hash=compute_sequence_hash(seq),
        )

    for raw in fasta_text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith(">"):
            record = flush()
            if record is not None:
                yield record
            header = line[1:]
            seq_lines = []
        else:
            seq_lines.append(line)

    record = flush()
    if record is not None:
        yield record


def _decode_response_body(content: bytes, compressed: bool) -> str:
    """Decode the raw HTTP body, gunzipping when ``compressed`` is True."""
    if compressed:
        with gzip.GzipFile(fileobj=BytesIO(content)) as f:
            return f.read().decode("utf-8", errors="replace")
    return content.decode("utf-8", errors="replace")


def _build_search_url(payload: UniProtFastaStreamPayload, cursor: str | None) -> str:
    encoded_query = quote(payload.search_criteria)
    params = [
        "format=fasta",
        f"query={encoded_query}",
        f"size={payload.page_size}",
    ]
    if payload.include_isoforms:
        params.append("includeIsoform=true")
    if payload.compressed:
        params.append("compressed=true")
    base = f"{payload.base_url}?{'&'.join(params)}"
    return base if not cursor else f"{base}&cursor={cursor}"


class UniProtSource(AnnotationSource):
    """UniProt REST source (FASTA paginated + metadata TSV)."""

    name = "uniprot"
    version = "uniprot-rest"

    def __init__(self) -> None:
        # One client per plugin instance — connection pooling stays
        # effective across multi-page runs. Per-execution counters
        # are reset at the start of each ``stream_fasta`` invocation.
        self._client = UniProtRetryClient()

    def stream_fasta(
        self,
        payload: UniProtFastaStreamPayload,
        *,
        emit: Any,
    ) -> Iterator[UniProtProteinRecord]:
        """Yield :class:`UniProtProteinRecord` instances from UniProt FASTA.

        Cursor-based pagination: the first request hits
        ``payload.base_url``; subsequent pages append the
        ``cursor=...`` value extracted from the previous page's
        ``Link: ...; rel="next"`` header. Pagination terminates when
        no next-cursor is present.

        HTTP counters (``self._client.requests``,
        ``self._client.retries``) are reset at the start of the call
        and accessible to the operation via :attr:`http_counters`.
        """
        self._client.reset()
        emit(
            "source.uniprot_fasta.start",
            None,
            {"search_criteria": payload.search_criteria, "page_size": payload.page_size},
            "info",
        )

        next_cursor: str | None = None
        page = 0
        while True:
            page += 1
            url = _build_search_url(payload, next_cursor)
            emit(
                "source.uniprot_fasta.fetch_page_start",
                None,
                {"page": page, "has_cursor": bool(next_cursor)},
                "info",
            )
            resp = self._client.get_with_retries(url, payload, emit)
            text = _decode_response_body(resp.content, payload.compressed)
            page_records = list(parse_fasta_text(text))
            emit(
                "source.uniprot_fasta.fetch_page_done",
                None,
                {"page": page, "records": len(page_records)},
                "info",
            )
            yield from page_records

            next_cursor = extract_next_cursor(resp.headers.get("link", ""))
            if not next_cursor:
                break

    @property
    def http_counters(self) -> tuple[int, int]:
        """Return ``(requests, retries)`` for the most recent stream run.

        The operation's emit payload includes these counters so a job
        can be audited post-hoc for HTTP behaviour without scraping
        the retry events.
        """
        return self._client.requests, self._client.retries

    # -- Deprecated ABC methods ------------------------------------------

    def stream(
        self,
        payload: dict[str, Any],
        *,
        emit: Any,
    ) -> Iterator[UniProtProteinRecord]:
        """Removed: this source has no single ``stream`` modality.

        UniProt has two modalities (FASTA via :meth:`stream_fasta`,
        metadata via :meth:`fetch_metadata` once F2A.6-real step 4
        lands). Callers must dispatch to the specific method.
        """
        raise NotImplementedError(
            "UniProtSource.stream is not implemented; use "
            "stream_fasta(payload) for FASTA records or "
            "fetch_metadata(payload) (pending step 4) for TSV metadata."
        )

    def load(
        self,
        session: Any,
        payload: dict[str, Any],
        *,
        emit: Any,
    ) -> dict[str, Any]:
        """Deprecated. Use :meth:`stream_fasta`. Removal pending D-MIGR-06."""
        raise NotImplementedError(
            "UniProtSource.load is deprecated; use "
            "UniProtSource.stream_fasta(payload) and own the session "
            "in the calling operation. ABC removal of load() is "
            "scheduled for D-MIGR-06 of master plan v3."
        )


#: Module-level plugin instance discovered via the
#: ``protea.sources`` entry_points group.
plugin = UniProtSource()
