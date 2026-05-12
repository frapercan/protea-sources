"""InterProScan TSV adapter.

Pure parsing logic, no HTTP, no subprocess. Two entry shapes for the
caller:

* :func:`parse_interproscan_tsv` accepts either a filesystem path or an
  in-memory string blob. The path form is for offline workflows where
  the IP.1b CLI runner has already dropped a TSV next to the job; the
  string form is for unit tests and stream consumers.
* :func:`parse_interproscan_tsv_line` is the single-line workhorse —
  splitting, column extraction, and the per-cell normalisation rules
  live here so unit tests can pin parser behaviour without staging a
  file.

InterProScan TSV emits one row per domain hit per analysis. A protein
with three Pfam hits and two Gene3D hits produces five rows. Columns
12 through 15 (InterPro accession, description, GO, pathways) are
optional and only present when InterProScan was invoked with
``-iprlookup`` / ``-goterms`` / ``-pa``; rows without an InterPro
mapping carry empty strings (or literal ``"-"``) in those columns.

The optional ``#`` comment header is InterProScan's release-version
marker (``# InterProScan-5.66-98.0`` style). The parser strips it from
the data stream and exposes it via
:func:`parse_release_version_header`.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Iterator
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

# InterProScan TSV column indices (0-based). Names mirror the official
# column order at
# https://interproscan-docs.readthedocs.io/en/latest/OutputFormats.html
_IDX_ACCESSION = 0
_IDX_ANALYSIS = 3
_IDX_START = 6
_IDX_END = 7
_IDX_SCORE = 8
_IDX_IPR_ACCESSION = 11

# Columns 1..11 are always present; columns 12..15 are optional.
_MIN_COLUMNS = 11

# Cells InterProScan writes when a value is absent.
_NULL_TOKENS = frozenset({"", "-"})


class InterProAnnotation(BaseModel):
    """One InterProScan domain hit, normalised for PROTEA persistence.

    Field set mirrors the IP.1 slice spec: seven columns covering the
    source DB, target protein, hit coordinates, hit confidence, and
    both the InterPro entry the hit maps to and the InterProScan
    release that produced it.

    The record is frozen + strict + ``extra="forbid"`` to match the
    other source records (:class:`GoaAnnotationRecord` etc.) so any
    drift between the parser and downstream operations fails at the
    boundary instead of in the persistence layer.
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    source_db: str
    """TSV column 4 (Analysis): the member database that produced the
    hit (e.g. ``"Pfam"``, ``"Gene3D"``, ``"PRINTS"``)."""

    accession: str
    """TSV column 1 (Protein accession): UniProt-style accession or
    whatever ID the input FASTA carried."""

    start: int = Field(gt=0)
    """TSV column 7: 1-based inclusive start of the domain hit on the
    target sequence."""

    end: int = Field(gt=0)
    """TSV column 8: 1-based inclusive end of the domain hit. Must be
    >= ``start`` (enforced at construction time)."""

    evidence: str | None = None
    """TSV column 9 (Score / e-value): hit confidence as the raw string
    InterProScan emitted. Member databases report different statistics
    (Pfam e-value vs PRINTS p-value vs Gene3D bit-score), so the
    plugin keeps the raw form; the operation casts to float when it
    needs to filter or rank."""

    ipr_version: str | None = None
    """TSV column 12 (InterPro accession): the InterPro entry the
    member-DB hit was integrated into (e.g. ``"IPR000123"``). ``None``
    when the signature has no integrated InterPro entry (column empty
    or literal ``"-"``)."""

    ipr_release_version: str | None = None
    """InterProScan release tag (e.g. ``"InterProScan-5.66-98.0"``).
    Parsed from the optional ``#``-prefixed header line of the TSV if
    present; otherwise supplied by the caller (the IP.1b CLI runner
    will pass it from ``interproscan.sh --version``)."""


def _normalise_optional(cell: str) -> str | None:
    """Map empty / ``"-"`` placeholder cells to ``None``.

    InterProScan writes ``"-"`` for absent integration mappings; tests
    in the wild occasionally show an empty cell instead. Both collapse
    to ``None`` so downstream code only has to check one sentinel.
    """
    stripped = cell.strip()
    return None if stripped in _NULL_TOKENS else stripped


def parse_release_version_header(line: str) -> str | None:
    """Extract the InterProScan release tag from a ``#`` header line.

    Returns ``None`` for non-comment lines so callers can use it as a
    filter inside a single-pass iterator. Recognised forms::

        # InterProScan-5.66-98.0
        #InterProScan version 5.66-98.0
    """
    if not line.startswith("#"):
        return None
    payload = line.lstrip("#").strip()
    if not payload:
        return None
    return payload


def parse_interproscan_tsv_line(
    line: str,
    *,
    release_version: str | None = None,
) -> InterProAnnotation | None:
    """Parse one TSV row into an :class:`InterProAnnotation`.

    Returns ``None`` (and the iterator skips silently) for:

    * Empty / whitespace-only lines.
    * Comment lines (``#`` prefix) — the caller should funnel those
      through :func:`parse_release_version_header` if it cares.
    * Rows with fewer than 11 tab-separated cells (malformed).
    * Rows whose ``start`` / ``end`` cells aren't valid integers or
      violate ``start <= end``.

    ``release_version`` is threaded through the record as-is; the
    plugin top-level reads it from the header line and forwards it.
    """
    stripped = line.rstrip("\n")
    if not stripped.strip() or stripped.startswith("#"):
        return None
    parts = stripped.split("\t")
    if len(parts) < _MIN_COLUMNS:
        return None
    try:
        start = int(parts[_IDX_START])
        end = int(parts[_IDX_END])
    except ValueError:
        return None
    if start <= 0 or end < start:
        return None
    ipr_accession: str | None = None
    if len(parts) > _IDX_IPR_ACCESSION:
        ipr_accession = _normalise_optional(parts[_IDX_IPR_ACCESSION])
    return InterProAnnotation(
        source_db=parts[_IDX_ANALYSIS],
        accession=parts[_IDX_ACCESSION],
        start=start,
        end=end,
        evidence=_normalise_optional(parts[_IDX_SCORE]),
        ipr_version=ipr_accession,
        ipr_release_version=release_version,
    )


def _iter_lines(source: str | os.PathLike[str] | Iterable[str]) -> Iterator[str]:
    """Yield lines from a path, a blob, or an already-iterable source.

    Handles the three forms documented on :func:`parse_interproscan_tsv`
    in a single place so the public parser stays a thin loop.
    """
    if isinstance(source, str | os.PathLike):
        candidate = Path(source)
        if candidate.exists() and candidate.is_file():
            with candidate.open(encoding="utf-8", errors="replace") as handle:
                yield from handle
            return
        if isinstance(source, str):
            yield from source.splitlines()
            return
        raise FileNotFoundError(f"InterProScan TSV path does not exist: {source}")
    yield from source


def parse_interproscan_tsv(
    source: str | os.PathLike[str] | Iterable[str],
) -> Iterator[InterProAnnotation]:
    """Parse an InterProScan TSV into a record iterator.

    ``source`` may be:

    * A :class:`pathlib.Path` (or ``os.PathLike``) to a TSV file on
      disk.
    * A multi-line :class:`str` containing the TSV body.
    * Any iterable of :class:`str` lines (for streamed input).

    The first encountered comment line (``#`` prefix) is treated as
    the release-version header and threaded into every emitted
    record's ``ipr_release_version`` field. Subsequent comment lines
    are skipped silently. When the source has no header, the release
    field stays ``None`` and the caller is expected to backfill it
    (the IP.1b runner will).
    """
    release_version: str | None = None
    for raw in _iter_lines(source):
        line = raw.rstrip("\n")
        if release_version is None:
            candidate = parse_release_version_header(line)
            if candidate is not None:
                release_version = candidate
                continue
        record = parse_interproscan_tsv_line(line, release_version=release_version)
        if record is not None:
            yield record
