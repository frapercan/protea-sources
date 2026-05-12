"""Typed input payload for :meth:`InterProSource.run`.

Lives in the plugin tree rather than ``protea-contracts`` for now. The
F2A.6-real migration pattern is: each new source ships its own
``<Name>StreamPayload`` / ``<Name>RunPayload`` next to the plugin until
the contracts package adopts it. Promoting :class:`InterProRunPayload`
into ``protea-contracts`` is a follow-up bookkeeping commit; doing it
here keeps IP.1b self-contained while still giving callers a typed
boundary (no ``dict[str, Any]`` payloads).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# Default per-run wall clock. InterProScan against a few thousand
# sequences routinely takes minutes; the operation layer overrides this
# when it knows the batch size.
_DEFAULT_TIMEOUT_SECONDS = 3600


class InterProRunPayload(BaseModel):
    """Inputs for one :meth:`InterProSource.run` invocation.

    Frozen + ``extra="forbid"`` so a typo (``fasta`` vs ``fasta_path``)
    fails at construction instead of silently invoking ``interproscan.sh``
    with a broken command line.
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    fasta_path: str
    """Filesystem path to the input FASTA passed via ``-i`` to
    ``interproscan.sh``. The plugin does not validate existence — the
    binary itself emits a clearer error than a Python pre-check would,
    and avoiding the stat keeps the runner pure-subprocess for the
    mock-based smoke test."""

    extra_args: list[str] = Field(default_factory=list)
    """Pass-through CLI flags appended after the canonical
    ``-i/-f/-o`` triple. Used by the operation layer to set ``-appl``
    (analysis subset), ``-iprlookup`` / ``-goterms`` / ``-pa`` (column
    12..15 enrichment), ``-cpu`` (parallelism), etc."""

    timeout_seconds: int = Field(default=_DEFAULT_TIMEOUT_SECONDS, gt=0)
    """Wall-clock timeout for the ``interproscan.sh`` subprocess.
    Enforced via ``subprocess.run(..., timeout=...)``; a timeout raises
    :class:`subprocess.TimeoutExpired` which the operation layer logs
    as a job failure."""

    binary_path: str | None = None
    """Explicit ``interproscan.sh`` path. ``None`` falls back to the
    ``PROTEA_INTERPROSCAN_BIN`` environment variable, then to the bare
    name ``interproscan.sh`` (resolved through ``$PATH``)."""
