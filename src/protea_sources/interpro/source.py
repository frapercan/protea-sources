"""InterProScan ``AnnotationSource`` implementation.

IP.1a delivered the plugin scaffold + the pure-Python TSV parser.
IP.1b adds the live runner: :meth:`InterProSource.run` invokes
``interproscan.sh`` via :mod:`subprocess`, writes the TSV to a
temp file (``-o <tempfile>``), then reads + parses it.  It threads
the ``--version`` tag onto every emitted record and yields parsed
:class:`InterProAnnotation` instances.

Why a temp file instead of stdout (``-o -``):

InterProScan 5.77 does NOT stream TSV to stdout when ``-o -`` is
given.  The flag is accepted without error, but stdout carries only
log lines and no data rows.  ``-o <real-file>`` on identical input
yields the correct TSV (verified: 95 hit rows over 6 proteins).
Using a :class:`tempfile.NamedTemporaryFile` keeps the fix
self-contained: the file is created before the subprocess call,
passed via ``-o``, read after the call, and deleted on context exit
regardless of success or failure.

Why the runner stays on the plugin (and not in the operation layer):

* ``protea-core`` reaches every source through the
  ``protea.sources`` entry-points group. Keeping the subprocess
  invocation here means the operation that loads InterProScan
  annotations is dispatch-only (it picks the plugin and pipes the
  records into the ORM); the operation does not need to know about
  binary paths, ``--version`` parsing, or stdout encoding.
* The :func:`subprocess.run` call is centralised so the timeout +
  ``check=True`` posture is uniform: a hang, a non-zero exit, or a
  missing binary all surface as exceptions that the runner layer
  already handles per the workflow's retry policy.

Out of scope for IP.1b (kept as docstring breadcrumbs):

* IP.1c: REST-API fallback when the binary is absent and the host
  has internet access (EBI InterProScan REST).
* IP.2: ORM model ``interpro_annotation`` in PROTEA core.
* IP.3: ``run_interproscan_batch`` operation wiring this into the
  job queue.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from protea_contracts import AnnotationSource

from protea_sources.interpro.interpro2go import (
    INTERPRO2GO_RELEASE,
    AncestorLookup,
    InterProGOPrediction,
    propagate_go_predictions,
)
from protea_sources.interpro.parser import (
    InterProAnnotation,
    parse_interproscan_tsv,
)
from protea_sources.interpro.payload import InterProRunPayload

# Environment-variable knob for the binary path. Mirrors the
# ``PROTEA_*`` prefix used elsewhere in the stack so the deploy
# tooling can set it alongside DB credentials in one ``.env``.
ENV_BINARY_PATH = "PROTEA_INTERPROSCAN_BIN"

# Default binary name when neither the payload nor the env var supplies
# one. Bare name → ``$PATH`` resolution at exec time.
_DEFAULT_BINARY = "interproscan.sh"

# Bounded timeout for ``interproscan.sh --version``. The flag prints a
# one-line banner and exits; anything slower than this is already a
# misconfiguration we want to surface, not silently wait on.
_VERSION_TIMEOUT_SECONDS = 30

# Hint surfaced in the ``RuntimeError`` when the binary is missing.
# Installation docs:
# https://interproscan-docs.readthedocs.io/en/latest/InstallationRequirements.html
_INSTALL_HINT = (
    "InterProScan is not installed or not on PATH. Install it from "
    "https://interproscan-docs.readthedocs.io/ or set the "
    f"{ENV_BINARY_PATH} environment variable to the absolute path of "
    "interproscan.sh."
)


@dataclass(frozen=True)
class _CommandSpec:
    """Internal value object: the binary path + the full argv vector.

    Split out of :meth:`InterProSource.run` so the argv construction
    stays a one-screen helper and the smell-budget method-LOC ceiling
    (60 LOC) is comfortable.
    """

    binary: str
    argv: tuple[str, ...]


def _resolve_binary(payload: InterProRunPayload) -> str:
    """Pick the ``interproscan.sh`` path with the documented fallback order.

    1. ``payload.binary_path`` (explicit caller override).
    2. ``$PROTEA_INTERPROSCAN_BIN`` (deploy-tool setting).
    3. Bare ``"interproscan.sh"`` (relies on ``$PATH``).
    """
    if payload.binary_path:
        return payload.binary_path
    env_value = os.environ.get(ENV_BINARY_PATH)
    if env_value:
        return env_value
    return _DEFAULT_BINARY


def _build_command(
    payload: InterProRunPayload, binary: str, output_path: str
) -> _CommandSpec:
    """Assemble the ``interproscan.sh`` argv for one run.

    ``output_path`` is a real filesystem path (typically a
    :class:`tempfile.NamedTemporaryFile` path) passed via ``-o``.
    InterProScan 5.77 does not stream TSV to stdout when ``-o -`` is
    used; ``-o <file>`` is the only reliable way to capture data rows.
    ``payload.extra_args`` is appended last so the caller can layer on
    ``-iprlookup`` / ``-goterms`` / ``-pa`` / ``-appl`` / ``-cpu``
    without the plugin needing first-class flags for each.
    """
    argv = (
        binary,
        "-i",
        payload.fasta_path,
        "-f",
        "tsv",
        "-o",
        output_path,
        *payload.extra_args,
    )
    return _CommandSpec(binary=binary, argv=argv)


def _capture_version(binary: str) -> str:
    """Run ``<binary> --version`` and return the first non-blank line.

    InterProScan prints a banner like ``InterProScan-5.66-98.0``
    followed by an optional copyright tail. We keep the first non-blank
    line so the value matches what the docs and the TSV header use.
    A missing binary surfaces as :class:`RuntimeError` here so callers
    do not have to special-case the same error twice (version probe +
    main run).
    """
    try:
        completed = subprocess.run(
            [binary, "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=_VERSION_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"{_INSTALL_HINT} (tried: {binary!r})") from exc
    stdout = completed.stdout or completed.stderr
    for line in stdout.splitlines():
        candidate = line.strip()
        if candidate:
            return candidate
    return ""


def _run_subprocess(
    payload: InterProRunPayload, binary: str, timeout_seconds: int
) -> str:
    """Invoke ``interproscan.sh`` and return the TSV content.

    InterProScan 5.77 does NOT write data rows to stdout when ``-o -``
    is used (only log lines appear).  This function creates a
    :class:`tempfile.NamedTemporaryFile`, passes its path via ``-o``,
    runs the subprocess, then reads + returns the file content.  The
    temp file is removed on context exit regardless of whether the
    subprocess succeeds or raises.

    ``check=True`` so a non-zero exit becomes
    :class:`subprocess.CalledProcessError`; the operation layer's retry
    policy decides whether to retry. The timeout is forwarded straight
    to :func:`subprocess.run` so a hung child process raises rather than
    blocking the worker forever.
    """
    with tempfile.NamedTemporaryFile(
        suffix=".tsv", prefix="protea_ips_", delete=True
    ) as tmp:
        output_path = tmp.name
        spec = _build_command(payload, binary, output_path)
        try:
            subprocess.run(
                list(spec.argv),
                check=True,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"{_INSTALL_HINT} (tried: {spec.binary!r})") from exc
        return Path(output_path).read_text(encoding="utf-8")


class InterProSource(AnnotationSource):
    """InterProScan domain-annotation source.

    ``name``/``version`` follow the convention set by
    :class:`GoaSource` and :class:`QuickGoSource`: a stable string id
    for ``protea-core`` dispatch and a human-readable version tag.
    The release-specific version (e.g. ``"InterProScan-5.66-98.0"``)
    is carried per-record in :attr:`InterProAnnotation.ipr_release_version`,
    not at the class level, because a single PROTEA deployment can
    re-index against multiple InterProScan releases over time.
    """

    name = "interpro"
    version = "interproscan-tsv"

    def parse_tsv(
        self,
        source: str | os.PathLike[str],
    ) -> Iterator[InterProAnnotation]:
        """Adapter shim around :func:`parse_interproscan_tsv`.

        Accepts a filesystem path or an in-memory TSV blob. Exists on
        the plugin class so ``protea-core`` callers can reach the
        parser through the discovered plugin instance without
        importing the parser module directly, keeping the import
        surface narrow as IP.1b/IP.2 add more methods.
        """
        yield from parse_interproscan_tsv(source)

    def run(
        self,
        payload: InterProRunPayload,
        *,
        emit: Any,
    ) -> Iterator[InterProAnnotation]:
        """Invoke ``interproscan.sh`` and yield parsed annotations.

        Workflow:

        1. Resolve the binary path (payload override → env var → PATH).
        2. Probe ``--version`` so every record carries the release tag
           that produced it (column-12-style provenance).
        3. Run ``interproscan.sh -i <fasta> -f tsv -o <tempfile>`` (plus
           ``payload.extra_args``) under a wall-clock timeout, read the
           output file, and parse it through :func:`parse_interproscan_tsv`.
           ``-o -`` (stdout) is intentionally NOT used: InterProScan 5.77
           emits only log lines to stdout with that flag and writes 0 data
           rows, causing every batch to insert 0 annotations silently.
        4. Override each record's ``ipr_release_version`` with the
           ``--version`` value so the field is populated even when the
           TSV file has no ``#``-header.

        ``emit(event, payload, fields, level)`` mirrors the GOA / UniProt
        plugins so ``protea-core`` can stream lifecycle events into the
        job-log queue without coupling to a logger instance.
        """
        binary = _resolve_binary(payload)
        release_version = _capture_version(binary)
        emit(
            "source.interpro.run_start",
            None,
            {"binary": binary, "release_version": release_version},
            "info",
        )
        tsv_content = _run_subprocess(payload, binary, payload.timeout_seconds)
        emit(
            "source.interpro.run_done",
            None,
            {"binary": binary, "tsv_bytes": len(tsv_content)},
            "info",
        )
        for record in parse_interproscan_tsv(tsv_content):
            yield record.model_copy(update={"ipr_release_version": release_version})

    def predict_go(
        self,
        annotations: Iterable[InterProAnnotation],
        *,
        ancestors: AncestorLookup,
        emit: Any,
    ) -> Iterator[InterProGOPrediction]:
        """Turn InterPro domain hits into ``(protein, go_id, score)`` GO predictions.

        Maps each hit's interpro2go GO terms
        (:attr:`InterProAnnotation.go_terms`, column 14) to a flat
        operating-point score and true-path-propagates every term up the
        ontology via ``ancestors`` (build it with
        :func:`~protea_sources.interpro.interpro2go.load_obo_ancestors`).
        When the same GO id is reached more than once the maximum score
        wins; root terms are dropped. Each yielded
        :class:`InterProGOPrediction` carries the pinned
        :data:`INTERPRO2GO_RELEASE` for provenance.

        ``emit(event, payload, fields, level)`` mirrors :meth:`run` so
        ``protea-core`` can stream lifecycle events without coupling to a
        logger. The ``predict_go_done`` event reports the prediction
        count once the consumer has drained the generator.
        """
        emit(
            "source.interpro.predict_go_start",
            None,
            {"interpro2go_release": INTERPRO2GO_RELEASE},
            "info",
        )
        count = 0
        for prediction in propagate_go_predictions(annotations, ancestors=ancestors):
            count += 1
            yield prediction
        emit(
            "source.interpro.predict_go_done",
            None,
            {"predictions": count, "interpro2go_release": INTERPRO2GO_RELEASE},
            "info",
        )
