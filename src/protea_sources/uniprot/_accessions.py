"""The accessions endpoint's two limits, and the batches that respect both.

UniProt's batch endpoint (:data:`ACCESSIONS_URL`) answers a list of accessions with
one request and refuses that request on either of two limits: more than
:data:`MAX_ACCESSIONS_PER_REQUEST` accessions, or a request target (path and query)
longer than :data:`MAX_REQUEST_TARGET` characters. Which one binds depends on the
accessions, six characters for most Swiss-Prot entries and ten for most TrEMBL
ones, and on how many fields the request asks for. No fixed count stands for both,
so :func:`accession_batches` cuts by both.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from collections.abc import Sequence as Seq
from urllib.parse import urlsplit

#: Endpoint that answers a batch of accessions with one request.
ACCESSIONS_URL = "https://rest.uniprot.org/uniprotkb/accessions"

#: Hard limit of :data:`ACCESSIONS_URL`. Asking for 1001 answers "Only '1000'
#: accessions are allowed in each request". Measured against the service, not
#: assumed. This is a fact about UniProt, not a tunable: it does not belong in a
#: caller's payload, because a caller cannot choose it.
MAX_ACCESSIONS_PER_REQUEST = 1000

#: The other hard limit of :data:`ACCESSIONS_URL`, on the request target (path and
#: query), which grows with every accession and every field asked for. Measured
#: 2026-11-22 against the service: a target of 8,191 characters is answered, one of
#: 8,202 is refused with 414 Request-URI Too Long. With 26 metadata fields that is
#: 713 ten-character accessions per request, not 1,000. A fact about UniProt, like
#: the limit above.
MAX_REQUEST_TARGET = 8191


def accessions_url(accessions: Seq[str], fields: str) -> str:
    """The one request a batch of accessions is asked with."""
    return f"{ACCESSIONS_URL}?accessions={','.join(accessions)}&fields={fields}&format=tsv"


def request_target(url: str) -> int:
    """Characters in the path and query of ``url``, which is what the 414 limit counts."""
    parts = urlsplit(url)
    return len(parts.path) + 1 + len(parts.query)


def fitting_url(accessions: Seq[str], fields: str) -> str:
    """The batch's URL, refused here when UniProt would refuse it.

    Named here rather than left to UniProt's 400 or 414, so the message points at
    the caller's batching instead of at the service.

    :raises ValueError: if the batch breaks either limit.
    """
    if len(accessions) > MAX_ACCESSIONS_PER_REQUEST:
        raise ValueError(
            f"{len(accessions)} accessions exceeds UniProt's limit of "
            f"{MAX_ACCESSIONS_PER_REQUEST} per request; cut them with accession_batches"
        )
    url = accessions_url(accessions, fields)
    if (target := request_target(url)) > MAX_REQUEST_TARGET:
        raise ValueError(
            f"a request target of {target} characters exceeds UniProt's limit of "
            f"{MAX_REQUEST_TARGET}; cut the accessions with accession_batches"
        )
    return url


def accession_batches(accessions: Iterable[str], *, fields: str) -> Iterator[list[str]]:
    """Cut accessions, in order, into batches that each fit one request.

    A batch closes when one more accession would break either limit, so every
    accession lands in exactly one batch and the batches concatenate back to the
    input.

    :raises ValueError: if a single accession does not fit a request with these
        fields, which no batching can fix.
    """
    room = MAX_REQUEST_TARGET - request_target(accessions_url([], fields))
    batch: list[str] = []
    used = 0
    for accession in accessions:
        cost = len(accession) + (1 if batch else 0)
        if batch and (len(batch) == MAX_ACCESSIONS_PER_REQUEST or used + cost > room):
            yield batch
            batch, used, cost = [], 0, len(accession)
        if cost > room:
            raise ValueError(
                f"accession {accession!r} does not fit one request with "
                f"{len(fields)} characters of fields"
            )
        batch.append(accession)
        used += cost
    if batch:
        yield batch
