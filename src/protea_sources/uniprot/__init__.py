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

The plugin exposes three modality-specific stream methods:

* :meth:`UniProtSource.stream_fasta` yields
  :class:`UniProtProteinRecord` instances over UniProt FASTA pages.
* :meth:`UniProtSource.stream_release_fasta` yields the same records
  from a release directory's gzipped flat files, for result sets large
  enough that cursor pagination is rate-limited into impracticality.
* :meth:`UniProtSource.stream_metadata` yields
  :class:`UniProtMetadataRecord` instances over UniProt TSV pages
  (F2A.6-real step 4, UniProt metadata migration).

and two one-shot fetches, for callers that already know which accessions they
want and need an answer rather than a stream:

* :meth:`UniProtSource.fetch_accessions_tsv` for the batch endpoint.
* :meth:`UniProtSource.search_secondary_accessions` for the ``sec_acc:`` query,
  the only route that resolves secondary accessions.

Both exist so that no operation has to open its own socket. One did:
``ensure_goa_universe`` grew three endpoints and a second copy of the retry
client inside PROTEA, which is the migration this module's docstring already
describes as finished.

The generic :meth:`UniProtSource.stream` redirect raises
``NotImplementedError`` pointing callers at the specific method,
because UniProt has no single stream modality (unlike goa / quickgo).
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import re
from collections.abc import Iterable, Iterator
from collections.abc import Sequence as Seq
from dataclasses import dataclass
from io import BytesIO, StringIO, TextIOWrapper
from typing import Any
from urllib.parse import quote

from protea_contracts import (
    AnnotationSource,
    UniProtFastaStreamPayload,
    UniProtMetadataRecord,
    UniProtMetadataStreamPayload,
    UniProtProteinRecord,
    compute_sequence_hash,
    parse_isoform,
)

from protea_sources.uniprot._http import UniProtRetryClient, extract_next_cursor

#: Endpoint that answers a batch of accessions with one request.
ACCESSIONS_URL = "https://rest.uniprot.org/uniprotkb/accessions"
#: Search endpoint, the only route that resolves SECONDARY accessions.
SEARCH_URL = "https://rest.uniprot.org/uniprotkb/search"

#: Hard limit of :data:`ACCESSIONS_URL`. Asking for 1001 answers "Only '1000'
#: accessions are allowed in each request". Measured against the service, not
#: assumed. This is a fact about UniProt, not a tunable: it does not belong in a
#: caller's payload, because a caller cannot choose it.
MAX_ACCESSIONS_PER_REQUEST = 1000
#: Cap on OR conditions per search query, stated by UniProt in the body of its
#: 400: "Too many OR conditions in query. Maximum allowed is 100." Same
#: reasoning as above.
MAX_OR_CONDITIONS = 100

#: Search endpoint of UniParc, which keeps the sequence of an accession
#: UniProtKB has stopped serving.
UNIPARC_SEARCH_URL = "https://rest.uniprot.org/uniparc/search"
#: Cap on OR conditions for :data:`UNIPARC_SEARCH_URL`, which is NOT the 100 of
#: :data:`MAX_OR_CONDITIONS` and does not announce itself: measured 2026-10-11,
#: 50 conditions answer normally while 200 answer 200 OK with an empty body and
#: no error. A silent empty answer is the worst failure shape there is, so the
#: ValueError below fires before the request rather than after it.
MAX_UNIPARC_OR_CONDITIONS = 50

_RE_OS = re.compile(r"\bOS=([^=]+?)\sOX=")
_RE_OX = re.compile(r"\bOX=(\d+)")
_RE_GN = re.compile(r"\bGN=([^\s]+)")


@dataclass
class RetryKnobs:
    """Transport settings for the one-shot fetch methods.

    NOT frozen, and that is a constraint rather than a choice: the private
    ``_RetryKnobs`` Protocol that :class:`UniProtRetryClient` accepts declares
    its members as settable variables, which pydantic's frozen models satisfy
    and a frozen dataclass does not. Nothing mutates an instance of this; if the
    Protocol is ever narrowed to read-only properties, freeze this too.

    The streaming methods take their knobs from a contract payload, because
    those payloads describe a JOB. A one-shot fetch is not a job: the caller
    already has a payload of its own, and threading a second contract type
    through for six transport numbers would put transport into the contract.
    So these are a plain value object with the measured defaults, which the
    caller may replace.
    """

    user_agent: str = "PROTEA/protea-sources"
    timeout_seconds: int = 120
    max_retries: int = 6
    backoff_base_seconds: float = 2.0
    backoff_max_seconds: float = 60.0
    jitter_seconds: float = 0.5


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


def parse_fasta_lines(lines: Iterable[str]) -> Iterator[UniProtProteinRecord]:
    """Parse a stream of UniProt FASTA lines into a record iterator.

    Header lines start with ``>``; subsequent non-empty lines are
    sequence content. Whitespace inside the sequence is stripped.
    Records with empty sequences are skipped silently (UniProt
    occasionally returns malformed entries).

    This is the line-oriented form, so a caller holding a file handle
    or a decompressing stream never has to materialise the whole
    FASTA as one string. :func:`parse_fasta_text` wraps it for the
    in-memory case.
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

    for raw in lines:
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


def parse_fasta_text(fasta_text: str) -> Iterator[UniProtProteinRecord]:
    """Parse a UniProt FASTA string into a record iterator.

    Thin wrapper over :func:`parse_fasta_lines` for callers that
    already hold the whole text, such as a paginated REST response.
    """
    yield from parse_fasta_lines(fasta_text.splitlines())


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


def _build_metadata_url(payload: UniProtMetadataStreamPayload, cursor: str | None) -> str:
    encoded_query = quote(payload.search_criteria)
    params = [
        "format=tsv",
        f"query={encoded_query}",
        f"size={payload.page_size}",
        "compressed=true" if payload.compressed else "compressed=false",
        f"fields={quote(','.join(payload.fields))}",
    ]
    base = f"{payload.base_url}?{'&'.join(params)}"
    return base if not cursor else f"{base}&cursor={cursor}"


@dataclass(frozen=True)
class UniParcRow:
    """One UniParc entry, as its TSV row carries it.

    A deliberately plain record and not a contract type, for the same reason
    :meth:`UniProtSource.search_secondary_accessions` answers raw: what a row
    MEANS for a given accession is a decision, not transport. Two of them, in
    fact, and both are the caller's.

    The first is WHICH ROW. One accession can answer several rows, one per
    sequence version UniParc has ever seen: measured 2026-10-11 over a random
    sample of 600 accessions that UniProtKB no longer serves, 670 rows came
    back. ``first_seen`` and ``last_seen`` are here so the caller can pick the
    version that was current at the moment it cares about, instead of being
    handed one chosen by this module.

    The second is WHICH ACCESSIONS. ``accessions`` lists every UniProtKB entry
    that shares this exact sequence, so a row answers for accessions nobody
    asked about, and the caller has to intersect with its own batch. The
    entries usually carry a version suffix (``A0A014NDJ0.1``) and sometimes do
    not (``A0A7D5Z9H9``); both forms are passed through unchanged rather than
    normalised here, because the version is information and dropping it would
    be this module deciding.
    """

    upi: str
    accessions: tuple[str, ...]
    first_seen: str
    last_seen: str
    length: int | None
    sequence: str


def parse_uniparc_tsv(tsv_text: str) -> Iterator[UniParcRow]:
    """Parse a UniParc TSV string into :class:`UniParcRow` instances.

    Driven by the header through :class:`csv.DictReader`, so the caller's
    ``fields`` order does not matter and a column it did not ask for is simply
    absent rather than shifting the others. A row with no ``Entry`` or no
    ``Sequence`` is skipped: a UniParc entry without a sequence is the one thing
    this route exists to provide, so it is not a partial answer, it is noise.
    """
    reader = csv.DictReader(StringIO(tsv_text), delimiter="\t")
    for row in reader:
        celda = {k: (v if v is not None else "") for k, v in row.items()}
        upi = celda.get("Entry", "").strip()
        secuencia = celda.get("Sequence", "").strip()
        if not upi or not secuencia:
            continue
        crudas = celda.get("UniProtKB", "")
        largo = celda.get("Length", "").strip()
        yield UniParcRow(
            upi=upi,
            accessions=tuple(a.strip() for a in crudas.split(";") if a.strip()),
            first_seen=celda.get("First seen", "").strip(),
            last_seen=celda.get("Last seen", "").strip(),
            length=int(largo) if largo.isdigit() else None,
            sequence=secuencia,
        )


def parse_metadata_tsv(tsv_text: str) -> Iterator[UniProtMetadataRecord]:
    """Parse a UniProt TSV string into a record iterator.

    Uses :class:`csv.DictReader` so column-name → cell mapping handles
    quoted fields and commas correctly. Empty cells are normalised to
    ``""`` (never ``None``) so the operation can safely apply
    ``.strip()`` without optional-handling. Rows missing the
    ``Entry`` column are skipped silently.
    """
    reader = csv.DictReader(StringIO(tsv_text), delimiter="\t")
    for row in reader:
        normalised = {k: (v if v is not None else "") for k, v in row.items()}
        accession = normalised.get("Entry", "").strip()
        if not accession:
            continue
        yield UniProtMetadataRecord(
            accession=accession,
            raw_fields=normalised,
        )


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

    def stream_release_fasta(
        self,
        urls: Seq[str],
        *,
        payload: UniProtFastaStreamPayload,
        emit: Any,
    ) -> Iterator[UniProtProteinRecord]:
        """Yield records from UniProt's published release flat files.

        Each URL names one gzipped FASTA from a UniProt release
        directory, typically ``uniprot_sprot.fasta.gz`` (the canonical
        Swiss-Prot entries) plus ``uniprot_sprot_varsplic.fasta.gz``
        (their isoform sequences). Together they are the materialised
        result of ``reviewed:true``, so they carry the same records
        that :meth:`stream_fasta` would walk page by page.

        The difference is cost, not content. Cursor pagination over a
        result set of that size is progressively rate-limited by
        UniProt: throughput decays over the walk, which makes the
        wall-clock time of a full fetch unbounded in practice. The
        release files are static and served at full bandwidth.

        They are also more reproducible: a release directory is
        immutable and hash-published, so the caller can pin and verify
        the exact bytes a corpus was built from, which a live query
        against a moving database cannot offer. Each file's md5 and
        byte count are emitted on its ``file_done`` event, matching
        what the release directory's ``RELEASE.metalink`` publishes, so
        the caller's log identifies the bytes and not merely the URL.
        Pinning is still the caller's job -- this method takes the URLs
        it is given and does not resolve ``current_release``.

        Bodies are gunzipped incrementally, so peak memory is one
        compressed file rather than the decompressed text. Retries,
        backoff and the HTTP counters are shared with
        :meth:`stream_fasta` via the same client; ``payload`` is read
        only for those transport knobs and its query fields are
        ignored.
        """
        self._client.reset()
        emit(
            "source.uniprot_release_fasta.start",
            None,
            {"files": len(urls)},
            "info",
        )

        for index, url in enumerate(urls, start=1):
            yield from self._stream_one_release_file(index, url, payload, emit)

    def _stream_one_release_file(
        self,
        index: int,
        url: str,
        payload: UniProtFastaStreamPayload,
        emit: Any,
    ) -> Iterator[UniProtProteinRecord]:
        """Fetch and parse one gzipped release file, counting its records."""
        emit(
            "source.uniprot_release_fasta.file_start",
            None,
            {"file": index, "url": url},
            "info",
        )
        resp = self._client.get_with_retries(url, payload, emit)
        # md5 of the compressed bytes, which is what a release
        # directory's RELEASE.metalink publishes. Emitting it puts the
        # identity of the exact bytes in the caller's event log, so a
        # corpus can be traced to them even after ``current_release``
        # has moved on. Integrity against a published checksum, not a
        # security digest.
        digest = hashlib.md5(resp.content, usedforsecurity=False).hexdigest()
        count = 0
        with gzip.GzipFile(fileobj=BytesIO(resp.content)) as raw:
            text = TextIOWrapper(raw, encoding="utf-8", errors="replace")
            for record in parse_fasta_lines(text):
                count += 1
                yield record
        emit(
            "source.uniprot_release_fasta.file_done",
            None,
            {"file": index, "records": count, "md5": digest, "bytes": len(resp.content)},
            "info",
        )

    def fetch_accessions_tsv(
        self,
        accessions: Seq[str],
        *,
        fields: str,
        emit: Any,
        knobs: RetryKnobs | None = None,
    ) -> str:
        """Fetch one batch of accessions as TSV, with the requested fields.

        One request for the whole batch, which is what makes this endpoint the
        fast route: measured 0,2 to 0,9 s per thousand accessions, against a
        cursor walk of the search endpoint that UniProt rate-limits into tens of
        hours for a result set of this size.

        IT MATCHES PRIMARY ACCESSIONS ONLY, and says nothing about it. A
        secondary accession is simply absent from the body while
        ``X-Total-Results`` still counts it, so a caller that trusts the header
        reads a complete answer where half the rows are missing. Resolving those
        is :meth:`search_secondary_accessions`, deliberately a separate call so
        the two failure modes stay separate.

        An accession the service no longer serves is likewise absent rather than
        erroring: an entry deleted from UniProt answers 200 on its own URL with
        ``entryType: Inactive`` and no sequence, and is omitted here. So the
        answer being shorter than the request is normal and means "gone", not
        "failed".

        :raises ValueError: if the batch exceeds
            :data:`MAX_ACCESSIONS_PER_REQUEST`. Named here rather than left to
            UniProt's 400, so the message points at the caller's chunking
            instead of at the service.
        :raises RuntimeError: on a non-transient status or exhausted retries.
            A batch that failed is never reported as a batch that found nothing:
            an earlier measurement read 0% recoverable merges because ten
            batches had 400'd and the failures were tallied as zeroes.
        """
        if len(accessions) > MAX_ACCESSIONS_PER_REQUEST:
            raise ValueError(
                f"{len(accessions)} accessions exceeds UniProt's limit of "
                f"{MAX_ACCESSIONS_PER_REQUEST} per request; chunk before calling"
            )
        k = knobs or RetryKnobs()
        url = f"{ACCESSIONS_URL}?accessions={','.join(accessions)}&fields={fields}&format=tsv"
        emit(
            "source.uniprot_accessions.fetch_start",
            None,
            {"accessions": len(accessions)},
            "info",
        )
        resp = self._client.get_with_retries(url, k, emit)
        return resp.content.decode("utf-8", errors="replace")

    def search_secondary_accessions(
        self,
        accessions: Seq[str],
        *,
        emit: Any,
        knobs: RetryKnobs | None = None,
    ) -> dict[str, Any]:
        """Resolve SECONDARY accessions through the search endpoint.

        One ``sec_acc:`` query per batch, without ``fields``, deliberately.
        Asking for ``fields=accession,sec_acc`` answers 400 ``Invalid fields
        parameter value 'sec_acc'``: it is a valid QUERY field but not a RETURN
        field, so the mapping has to be read from each entry's own
        ``secondaryAccessions`` list in the full document.

        The answer is returned raw. Deciding what a hit MEANS is the caller's
        job and not transport's: an accession with one successor is a merge and
        can be aliased, while one with several is a demerge and has no single
        successor, so aliasing it would invent a curatorial decision. The
        caller also has to group by the accession it asked for, because one
        entry can absorb several of them.

        :raises ValueError: if the batch exceeds :data:`MAX_OR_CONDITIONS`.
        :raises RuntimeError: as in :meth:`fetch_accessions_tsv`.
        """
        if len(accessions) > MAX_OR_CONDITIONS:
            raise ValueError(
                f"{len(accessions)} OR conditions exceeds UniProt's limit of "
                f"{MAX_OR_CONDITIONS}; chunk before calling"
            )
        import json
        from urllib.parse import quote

        k = knobs or RetryKnobs()
        q = " OR ".join(f"sec_acc:{a}" for a in accessions)
        url = f"{SEARCH_URL}?query={quote(q)}&format=json&size=500"
        emit(
            "source.uniprot_sec_acc.search_start",
            None,
            {"accessions": len(accessions)},
            "info",
        )
        resp = self._client.get_with_retries(url, k, emit)
        cuerpo = resp.content.decode("utf-8", errors="replace")
        return json.loads(cuerpo)  # type: ignore[no-any-return]

    def search_uniparc_tsv(
        self,
        accessions: Seq[str],
        *,
        fields: str,
        emit: Any,
        knobs: RetryKnobs | None = None,
    ) -> str:
        """Fetch from UniParc the sequences UniProtKB no longer serves.

        THE GAP THIS FILLS. :meth:`fetch_accessions_tsv` omits an accession the
        service has stopped serving, and its docstring says so. What it does not
        say is that the accession is not gone from the world: UniParc keeps
        every sequence UniProtKB has ever held, deleted entries included.
        Measured 2026-10-11 on ``A0A014NDJ0``, one of 28.868 that a full
        resolution pass could not place: its own UniProtKB URL answers 200 with
        an empty body, the batch endpoint answers 200 with
        ``X-Total-Results: 1`` and no data row, and UniParc answers it in full.
        Over a random sample of 600 of those 28.868, this route resolved 600.

        WHY TSV AND NOT FASTA. The FASTA header carries only the UPI
        (``>UPI0001FE00B9 status=active``), so a batched answer cannot be
        mapped back to the accessions that were asked for. The TSV carries the
        cross-reference, the dates and the sequence in one request, which is
        what makes one pass enough.

        The answer is returned raw, as in
        :meth:`search_secondary_accessions`: :func:`parse_uniparc_tsv` turns it
        into rows, and :class:`UniParcRow` documents the two decisions the
        caller is left with.

        :raises ValueError: if the batch exceeds
            :data:`MAX_UNIPARC_OR_CONDITIONS`. Named here because UniParc does
            NOT answer 400 on too many conditions: it answers 200 with an empty
            body, so a caller that chunked wrong would read "none of these
            exist" and record 50 proteins as unrecoverable.
        :raises RuntimeError: as in :meth:`fetch_accessions_tsv`.
        """
        if len(accessions) > MAX_UNIPARC_OR_CONDITIONS:
            raise ValueError(
                f"{len(accessions)} OR conditions exceeds UniParc's limit of "
                f"{MAX_UNIPARC_OR_CONDITIONS}; chunk before calling"
            )
        from urllib.parse import quote

        k = knobs or RetryKnobs()
        q = " OR ".join(accessions)
        url = f"{UNIPARC_SEARCH_URL}?query={quote(q)}&format=tsv&fields={quote(fields)}&size=500"
        emit(
            "source.uniparc.search_start",
            None,
            {"accessions": len(accessions), "fields": fields},
            "info",
        )
        resp = self._client.get_with_retries(url, k, emit)
        return resp.content.decode("utf-8", errors="replace")

    def stream_metadata(
        self,
        payload: UniProtMetadataStreamPayload,
        *,
        emit: Any,
    ) -> Iterator[UniProtMetadataRecord]:
        """Yield :class:`UniProtMetadataRecord` instances from UniProt TSV.

        Cursor pagination identical to :meth:`stream_fasta`; the only
        differences are ``format=tsv``, the ``fields=...`` query param
        carrying the requested column list, and gzip compression
        defaulting to ``True`` (TSV responses are large).

        HTTP counters share the same client as ``stream_fasta``, so a
        run that interleaves both calls accumulates into the same
        ``http_counters`` tuple — the operation should call
        ``self._client.reset()`` (or accept the running totals) per
        execution as appropriate.
        """
        self._client.reset()
        emit(
            "source.uniprot_metadata.start",
            None,
            {"search_criteria": payload.search_criteria, "page_size": payload.page_size},
            "info",
        )

        next_cursor: str | None = None
        page = 0
        while True:
            page += 1
            url = _build_metadata_url(payload, next_cursor)
            emit(
                "source.uniprot_metadata.fetch_page_start",
                None,
                {"page": page, "has_cursor": bool(next_cursor)},
                "info",
            )
            resp = self._client.get_with_retries(url, payload, emit)
            text = _decode_response_body(resp.content, payload.compressed)
            page_records = list(parse_metadata_tsv(text))
            emit(
                "source.uniprot_metadata.fetch_page_done",
                None,
                {"page": page, "rows": len(page_records)},
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

    # -- Generic stream redirect ----------------------------------------

    def stream(
        self,
        payload: dict[str, Any],
        *,
        emit: Any,
    ) -> Iterator[UniProtProteinRecord]:
        """No single ``stream`` modality on this source.

        UniProt has two modalities — FASTA via :meth:`stream_fasta`,
        metadata via :meth:`stream_metadata`. The redirect is kept (vs
        leaving the method absent) so callers that probe ``stream`` get
        a clear pointer to the specific method instead of an
        AttributeError. Other AnnotationSource plugins (goa, quickgo)
        do have a single ``stream`` method.
        """
        raise NotImplementedError(
            "UniProtSource.stream is not implemented; use "
            "stream_fasta(payload) for FASTA records or "
            "stream_metadata(payload) for TSV metadata."
        )


#: Module-level plugin instance discovered via the
#: ``protea.sources`` entry_points group.
plugin = UniProtSource()
