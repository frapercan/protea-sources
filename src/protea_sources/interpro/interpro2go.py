"""interpro2go mapping + true-path propagation for the InterPro source.

IP.1a/IP.1b parse InterProScan TSV into domain hits
(:class:`~protea_sources.interpro.parser.InterProAnnotation`). This
module turns those hits into ``(protein, go_id, score)`` GO predictions,
which is the shape the reranker / ensemble layer consumes.

Two steps, ported from the validated reference pipeline:

1. **interpro2go extract.** Each InterProScan hit carries the GO terms
   its signature maps to via the interpro2go release (TSV column 14,
   captured into :attr:`InterProAnnotation.go_terms` when InterProScan
   is run with ``-goterms``). A directly asserted GO term gets a flat
   ``direct_score``: InterPro hits are binary domain evidence (high
   precision), so the score is a constant operating point for InterPro
   alone, which the ensemble step rescales per (category, aspect).

2. **True-path propagation.** Each mapped GO term implies all of its
   ancestors (``is_a`` + ``part_of``). The standard true-path rule
   propagates the score up the ontology, where every ancestor inherits
   the maximum score of any descendant that implied it.

The interpro2go release is pinned in :data:`INTERPRO2GO_RELEASE` (a
single current release, overridable via ``PROTEA_INTERPRO2GO_RELEASE``)
and stamped onto every prediction for reproducibility. InterPro is an
allowed analysis tool, so a current release is correct: there is no
temporal-cutoff (``t0``) constraint on the mapping itself.
"""

from __future__ import annotations

import os
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from protea_sources.interpro.parser import InterProAnnotation

# Pinned interpro2go release. InterProScan applies the interpro2go
# mapping internally (``-goterms``) and writes the resulting GO terms
# into TSV column 14; this constant records the release we standardise
# on so a prediction set is always traceable to the exact mapping that
# produced it. Overridable for forward-compat without a code change.
_DEFAULT_INTERPRO2GO_RELEASE = "interpro2go/2025-09 (InterPro 105.0)"
ENV_INTERPRO2GO_RELEASE = "PROTEA_INTERPRO2GO_RELEASE"

INTERPRO2GO_RELEASE = os.environ.get(
    ENV_INTERPRO2GO_RELEASE, _DEFAULT_INTERPRO2GO_RELEASE
)

# Ontology roots (BP, MF, CC). Dropped from emitted predictions by
# default: a root term is true for every annotated protein and carries
# no discriminative signal.
GO_ROOTS = frozenset({"GO:0008150", "GO:0003674", "GO:0005575"})

# Flat operating-point score for a directly asserted InterPro GO term.
_DEFAULT_DIRECT_SCORE = 1.0
# Multiplier applied per propagation step. ``1.0`` keeps the full score
# on every ancestor (true-path rule); a value < 1.0 decays it with
# ontology depth.
_DEFAULT_PROP_DECAY = 1.0

# Callable that returns the transitively-closed ancestor set of a GO id.
AncestorLookup = Callable[[str], set[str]]


class InterProGOPrediction(BaseModel):
    """One ``(protein, go_id, score)`` GO prediction from InterPro.

    Frozen + strict + ``extra="forbid"`` to match the source records
    (:class:`InterProAnnotation`, :class:`GoaAnnotationRecord`) so any
    drift between this emitter and downstream consumers fails at the
    boundary instead of in the persistence / ensemble layer.
    """

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    accession: str
    """Target protein accession (InterProScan TSV column 1)."""

    go_id: str
    """Predicted GO id (``GO:nnnnnnn``), either directly asserted by an
    interpro2go mapping or implied via true-path propagation."""

    score: float = Field(ge=0.0)
    """Operating-point confidence in ``[0, 1]``. ``direct_score`` for a
    directly asserted term; the max inherited score for a propagated
    ancestor."""

    interpro2go_release: str
    """The :data:`INTERPRO2GO_RELEASE` tag that produced the mapping,
    carried per-record for provenance."""


def _parse_obo_ancestors(
    obo_path: str | os.PathLike[str],
) -> tuple[dict[str, set[str]], dict[str, str]]:
    """Read direct ``is_a`` + ``part_of`` parents and namespaces from an OBO.

    Returns ``(direct_parents, namespace)``. Obsolete terms are dropped.
    Transitive closure is deferred to :func:`_make_ancestor_lookup` so the
    parse stays a single linear pass.
    """
    direct: dict[str, set[str]] = defaultdict(set)
    namespace: dict[str, str] = {}
    cur: str | None = None
    in_term = False
    with Path(obo_path).open(encoding="utf-8") as handle:
        for raw in handle:
            line = raw.rstrip("\n")
            if line == "[Term]":
                in_term = True
                cur = None
                continue
            if line.startswith("[") and line.endswith("]") and line != "[Term]":
                in_term = False
                continue
            if not in_term:
                continue
            if line.startswith("id: GO:"):
                cur = line[4:].strip()
            elif line.startswith("namespace:") and cur:
                namespace[cur] = line.split(":", 1)[1].strip()
            elif line.startswith("is_a:") and cur:
                parent = line.split("is_a:", 1)[1].strip().split("!")[0].strip()
                if parent.startswith("GO:"):
                    direct[cur].add(parent)
            elif line.startswith("relationship: part_of") and cur:
                parent = line.split("part_of", 1)[1].strip().split("!")[0].strip()
                if parent.startswith("GO:"):
                    direct[cur].add(parent)
            elif line.startswith("is_obsolete: true") and cur:
                namespace.pop(cur, None)
                direct.pop(cur, None)
    return direct, namespace


def _make_ancestor_lookup(direct: dict[str, set[str]]) -> AncestorLookup:
    """Build a memoised, cycle-safe transitive-ancestor lookup."""
    memo: dict[str, set[str]] = {}

    def ancestors(term: str) -> set[str]:
        cached = memo.get(term)
        if cached is not None:
            return cached
        memo[term] = set()  # guard against cycles
        acc: set[str] = set()
        for parent in direct.get(term, ()):
            acc.add(parent)
            acc |= ancestors(parent)
        memo[term] = acc
        return acc

    return ancestors


def load_obo_ancestors(
    obo_path: str | os.PathLike[str],
) -> tuple[AncestorLookup, dict[str, str]]:
    """Load a GO OBO into an ancestor-lookup callable + a namespace map.

    The returned ``ancestors(go_id)`` yields the transitively-closed set
    of ``is_a`` + ``part_of`` ancestors (the true path to the root) for
    use with :func:`propagate_go_predictions`. ``namespace[go_id]`` maps
    a term to ``"biological_process"`` / ``"molecular_function"`` /
    ``"cellular_component"``.
    """
    direct, namespace = _parse_obo_ancestors(obo_path)
    return _make_ancestor_lookup(direct), namespace


def propagate_go_predictions(
    annotations: Iterable[InterProAnnotation],
    *,
    ancestors: AncestorLookup,
    direct_score: float = _DEFAULT_DIRECT_SCORE,
    prop_decay: float = _DEFAULT_PROP_DECAY,
    drop_roots: bool = True,
    release: str = INTERPRO2GO_RELEASE,
) -> Iterator[InterProGOPrediction]:
    """Map InterPro hits to GO and true-path-propagate into predictions.

    For each protein, every GO term on every hit
    (:attr:`InterProAnnotation.go_terms`, the interpro2go column) is
    asserted at ``direct_score`` and propagated to its ancestors at
    ``direct_score * prop_decay``. When the same GO id is reached more
    than once (multiple hits, or as an ancestor of several terms) the
    maximum score wins. Predictions are yielded sorted by protein then
    GO id for deterministic output; root terms are dropped by default.
    """
    pred: dict[str, dict[str, float]] = defaultdict(dict)
    for annotation in annotations:
        per_protein = pred[annotation.accession]
        for go_id in annotation.go_terms:
            if direct_score > per_protein.get(go_id, 0.0):
                per_protein[go_id] = direct_score
            propagated = direct_score * prop_decay
            for ancestor in ancestors(go_id):
                if propagated > per_protein.get(ancestor, 0.0):
                    per_protein[ancestor] = propagated

    for accession in sorted(pred):
        for go_id, score in sorted(pred[accession].items()):
            if drop_roots and go_id in GO_ROOTS:
                continue
            yield InterProGOPrediction(
                accession=accession,
                go_id=go_id,
                score=score,
                interpro2go_release=release,
            )
