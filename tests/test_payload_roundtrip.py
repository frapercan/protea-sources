"""Round-trip stability for the stream payloads protea-sources consumes.

Every source plugin in this package accepts a typed pydantic payload
on its ``stream`` / ``stream_fasta`` / ``stream_metadata`` /
``fetch_eco_mapping`` / ``run`` method (see ``test_contract_compliance.py``
for the ABC discovery). The payloads travel in ``Job.payload`` JSONB,
which means every type must round-trip through ``model_dump`` /
``model_validate`` without field loss.

This module exercises the JSONB round-trip from the consumer side so
a breaking change in ``protea_contracts.records`` (the canonical
payload shapes) or in ``protea_sources.interpro.payload`` (the
plugin-local payload that has not yet been promoted upstream) is
caught by the sources CI too, not only by the contracts CI.
"""

from __future__ import annotations

from typing import Any

import pytest
from protea_contracts import (
    EcoMappingPayload,
    GoaStreamPayload,
    QuickGoStreamPayload,
    UniProtFastaStreamPayload,
    UniProtMetadataStreamPayload,
)
from pydantic import BaseModel

from protea_sources.interpro.payload import InterProRunPayload

#: Canonical valid kwargs for every payload the source plugins consume.
#: When a new source is added with its own payload (e.g. an
#: ``InterPro`` -> ``contracts`` migration moves ``InterProRunPayload``
#: upstream), keep this table in sync so the coverage guard below
#: still passes.
CANONICAL_KWARGS: dict[type[BaseModel], dict[str, Any]] = {
    GoaStreamPayload: {"gaf_url": "https://example.org/goa.gaf.gz"},
    QuickGoStreamPayload: {"gene_product_ids": ["P12345", "Q67890"]},
    EcoMappingPayload: {"url": "https://example.org/eco.txt"},
    UniProtFastaStreamPayload: {"search_criteria": "reviewed:true"},
    UniProtMetadataStreamPayload: {
        "search_criteria": "reviewed:true",
        "fields": ["accession", "reviewed"],
    },
    InterProRunPayload: {"fasta_path": "/tmp/sample.fasta"},
}


@pytest.mark.parametrize(
    "cls, kwargs",
    list(CANONICAL_KWARGS.items()),
    ids=lambda v: v.__name__ if isinstance(v, type) else "kwargs",
)
def test_payload_roundtrip_python_mode(cls: type[BaseModel], kwargs: dict[str, Any]) -> None:
    """``Class.model_validate(instance.model_dump()) == instance`` per payload.

    The PROTEA worker pipeline serialises payloads to JSONB on enqueue
    and re-validates on dequeue; a payload that fails to round-trip
    silently loses fields with the worker only discovering it mid-job.
    """
    instance = cls.model_validate(kwargs)
    rebuilt = cls.model_validate(instance.model_dump())
    assert rebuilt == instance, (
        f"{cls.__name__} failed model_validate(model_dump()) round-trip; "
        "a default factory, custom validator, or extra-field policy may "
        "have drifted out of sync."
    )


@pytest.mark.parametrize(
    "cls, kwargs",
    list(CANONICAL_KWARGS.items()),
    ids=lambda v: v.__name__ if isinstance(v, type) else "kwargs",
)
def test_payload_roundtrip_json_mode(cls: type[BaseModel], kwargs: dict[str, Any]) -> None:
    """JSON-mode dump must also round-trip (Job.payload JSONB hot path)."""
    instance = cls.model_validate(kwargs)
    rebuilt = cls.model_validate(instance.model_dump(mode="json"))
    assert rebuilt == instance


def test_extra_forbid_blocks_typos() -> None:
    """The frozen+extra=forbid contract must reject typoed field names.

    Catches the regression where someone relaxes ``extra="forbid"`` to
    ``"ignore"`` on the source-side payloads; that would let a typo
    (``fasta`` vs ``fasta_path``) silently fall through to the
    interproscan subprocess as a broken command line.
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        InterProRunPayload.model_validate({"fasta_path": "/tmp/x", "fasta": "/tmp/y"})


def test_interpro_payload_field_defaults_documented() -> None:
    """``InterProRunPayload`` defaults must match the plugin contract.

    Pin-test: if someone changes ``timeout_seconds`` default to a
    smaller value, downstream long-running jobs (multi-thousand-sequence
    runs that legitimately take >1h) would silently start timing out.
    """
    p = InterProRunPayload.model_validate({"fasta_path": "/tmp/x.fasta"})
    assert p.extra_args == [], "extra_args default must be empty list"
    assert p.timeout_seconds == 3600, (
        "timeout_seconds default must remain 1h; smaller values break "
        "multi-thousand-sequence runs that legitimately take >1h."
    )
    assert p.binary_path is None, "binary_path default must be None (resolve via $PATH/env)"
