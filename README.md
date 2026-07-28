# protea-sources

**Annotation source plugins for the PROTEA stack.**
Each sub-module implements the `AnnotationSource` ABC from
[`protea-contracts`](https://github.com/frapercan/protea-contracts) and
registers via the `protea.sources` `entry_points` group so that
`protea-core` discovers it at startup without any code changes.

[![CI](https://github.com/frapercan/protea-sources/actions/workflows/ci.yml/badge.svg)](https://github.com/frapercan/protea-sources/actions/workflows/ci.yml)
[![Documentation](https://img.shields.io/readthedocs/protea-sources.svg)](https://protea-sources.readthedocs.io)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/)
[![License: Unlicense](https://img.shields.io/badge/license-Unlicense-blue.svg)](https://unlicense.org/)
[![PyPI](https://img.shields.io/pypi/v/protea-sources.svg)](https://pypi.org/project/protea-sources/)

**Status:** in use. All four source plugins are deployed and drive the
ingestion operations in the platform. The API may still change.
Every internal dependency in this stack now names a commit rather than a
branch, checked on each pull request. Updating one is a pull request here that
moves the commit, gated by this repository's own checks.

A definitive campaign run is being prepared for the doctoral thesis, and
earlier experimental results are being recomputed rather than carried forward.
No headline number is quoted in this file as current.

**Documentation:** [protea-sources.readthedocs.io](https://protea-sources.readthedocs.io)
(concepts and the annotation-source contract, quickstart, one page per source, full API reference).

---

<!-- protea-stack:start -->

## Repositories in the PROTEA stack

Single source of truth: [`docs/source/_data/stack.yaml`](https://github.com/frapercan/PROTEA/blob/develop/docs/source/_data/stack.yaml) in PROTEA. Run `python scripts/sync_stack.py` to regenerate this block.

| Repo | Role | Status | Summary |
|------|------|--------|---------|
| [PROTEA](https://github.com/frapercan/PROTEA) | Platform | `active` | Backend platform. Hosts the ORM, job queue, FastAPI surface, frontend, and orchestration. |
| [protea-contracts](https://github.com/frapercan/protea-contracts) | Contracts | `active` | Shared contract surface. ABCs, pydantic payloads, feature schema, schema_sha. Imported by every other repo. |
| [protea-method](https://github.com/frapercan/protea-method) | Inference | `active` | LAFA submission layer. Pure inference path (KNN, feature compute, reranker apply). Published to DockerHub; bind-mounted by LAFA containers. |
| **protea-sources** (this repo) | Source plugin | `active` | Annotation source plugins (GOA, QuickGO, UniProt, InterPro). Discovered via Python entry_points. |
| [protea-runners](https://github.com/frapercan/protea-runners) | Runner plugin | `active` | Experiment runner plugins (LightGBM, KNN, baseline). Discovered via Python entry_points. |
| [protea-backends](https://github.com/frapercan/protea-backends) | Backend plugin | `active` | Protein language model embedding backends (ESM family, T5/ProstT5, Ankh, ESM3-C). Discovered via Python entry_points. |
| [protea-reranker-lab](https://github.com/frapercan/protea-reranker-lab) | Lab | `active` | LightGBM reranker training lab. Pulls datasets from PROTEA, trains boosters, publishes them back via /reranker-models/import-by-reference. |
| [cafaeval-protea](https://github.com/frapercan/cafaeval-protea) | Evaluator | `active` | Standalone fork of cafaeval (CAFA-evaluator-PK) with the PK-coverage fix and a bit-exact parity guarantee against the upstream. |

<!-- protea-stack:end -->

---

## What this package does

A source plugin owns the **network and parsing** side of annotation
ingestion: downloading a release (GAF, TSV, FASTA, or InterProScan TSV),
streaming records, and yielding typed pydantic objects from
`protea-contracts`. Persistence (DB filtering, GO-term resolution, bulk
insert) stays in the `protea-core` operations that consume those streams.

The plugin/operation boundary is the typed record stream defined in
`protea-contracts.records`: plugins yield frozen pydantic models and
know nothing about the ORM; operations own the session, per-page
commits, and deduplication logic.

---

## Place in the stack

```
External databases
  └─ protea-sources   (download, parse, yield typed records)
        └─ protea-core operations   (filter, bulk insert, commit)
              └─ PROTEA job queue
```

`protea-core` resolves a source at runtime through
`importlib.metadata.entry_points`:

```python
from importlib.metadata import entry_points
plugin = entry_points(group="protea.sources")["goa"].load()
```

---

## Install

```bash
pip install protea-sources
```

No per-source extras: the upstream protocols (HTTP, gzip, TSV, FASTA)
are covered by `requests`, the only non-contracts runtime dependency.

---

## Quick example

```python
from protea_contracts import GoaStreamPayload
from protea_sources.goa import plugin as goa

emit = lambda *a, **kw: None  # discard structured-event callbacks
payload = GoaStreamPayload(gaf_url="https://example.com/small.gaf")

for record in goa.stream(payload, emit=emit):
    print(record.accession, record.go_id, record.evidence_code)
    # P12345 GO:0000123 IDA
```

QuickGO (TSV with optional ECO mapping):

```python
from protea_contracts import EcoMappingPayload, QuickGoStreamPayload
from protea_sources.quickgo import plugin as quickgo

eco_map = quickgo.fetch_eco_mapping(
    EcoMappingPayload(url="https://example.com/gaf-eco.txt"), emit=emit
)
for record in quickgo.stream(QuickGoStreamPayload(), emit=emit):
    code = eco_map.get(record.eco_id, record.eco_id)
```

InterProScan via local subprocess:

```python
from protea_sources.interpro import InterProRunPayload, plugin as interpro

# Pass -goterms so InterProScan fills the GO column (column 14) via interpro2go.
payload = InterProRunPayload(
    fasta_path="/data/proteins.fasta",
    extra_args=["-iprlookup", "-goterms"],
    timeout_seconds=3600,
)
for annotation in interpro.run(payload, emit=emit):
    print(annotation.accession, annotation.source_db, annotation.start, annotation.end)
```

The same plugin emits `(protein, go_id, score)` GO predictions by
mapping each hit's interpro2go GO terms and true-path-propagating them
up the ontology:

```python
from protea_sources.interpro import load_obo_ancestors, plugin as interpro

annotations = list(interpro.run(payload, emit=emit))
ancestors, _ = load_obo_ancestors("/data/go-basic.obo")
for pred in interpro.predict_go(annotations, ancestors=ancestors, emit=emit):
    print(pred.accession, pred.go_id, pred.score, pred.interpro2go_release)
```

Records are frozen pydantic models from `protea-contracts`: typos fail
at construction time, dataflow is one-way, schema drift is impossible.

---

## Sources

| Plugin | Source | Modality | Coverage |
|--------|--------|----------|----------|
| `protea_sources.goa` | UniProt-GOA bulk GAF (EBI FTP, HTTP + gzip) | GAF 2.x stream | 100% |
| `protea_sources.quickgo` | QuickGO REST API | TSV + ECO mapping | 100% |
| `protea_sources.uniprot` | UniProt REST | FASTA + metadata TSV | 100% / 93% (`_http.py`) |
| `protea_sources.interpro` | InterProScan subprocess + TSV parser | TSV stream | 100% (parser + payload) |

The `_http.py` retry client (UniProt) provides exponential backoff with
jitter, `Retry-After` header parsing, and `Link`-header cursor
extraction. Future sources that need retry logic reuse this client.

---

## Why a separate package

1. **Plugin extensibility.** New sources are added without touching
   `protea-core`. A single sub-module here plus one entry in
   `pyproject.toml` is all that is required.
2. **Self-contained.** HTTP, parsing, and source-specific retries
   live here. `protea-core` owns persistence; the boundary is the
   typed record stream defined in `protea-contracts`.
3. **Testable in isolation.** No DB, no SQLAlchemy. Parser tests run
   against canned bytes; HTTP tests run against `requests.get` mocks.

---

## Architecture

```
src/protea_sources/
    goa/              # HTTP + gzip + GAF 2.x parser
    quickgo/          # QuickGO REST, cursor pagination, ECO mapping
    uniprot/          # UniProt REST (FASTA + metadata); _http.py retry client
    interpro/
        parser.py     # pure TSV parsing (no subprocess, no network)
        payload.py    # InterProRunPayload typed input model
        source.py     # subprocess runner + version-header parsing
```

Full API reference and per-source operational notes (TSV column layout,
cursor extraction, ECO mapping, GAF quirks) live in the Sphinx docs:

```bash
poetry install --with docs
cd docs && make html
# open docs/build/html/index.html
```

---

## Versioning

SemVer 2.0.0. All sources share a release cycle because adding a new
source typically means adding new typed records to `protea-contracts`
as well, and the version bump there drives downstream rebuilds.

---

## Development

```bash
poetry install
poetry run pytest             # unit + parser tests, ~0.3 s
poetry run ruff check .
poetry run mypy --strict src
```

---

## Contributing

Contributions are welcome from research institutions and individual
developers. All changes target `develop`; `main` tracks stable releases.

```bash
git clone https://github.com/frapercan/protea-sources.git
cd protea-sources
git checkout develop
git checkout -b feature/my-source

poetry install
poetry run pytest
poetry run ruff check .
poetry run mypy --strict src
# Open a pull request targeting develop
```

**Adding a new source (five steps):**

1. Create `src/protea_sources/<name>/__init__.py` and subclass
   `AnnotationSource` from `protea-contracts`.
2. Add typed payload + record models to `protea-contracts` first
   (coordinated version bump), then consume them here.
3. Register the entry point under `[tool.poetry.plugins."protea.sources"]`
   in `pyproject.toml`.
4. Add tests in `tests/test_<name>.py` covering ABC compliance,
   entry-point discoverability, and parser correctness on fixture bytes.
5. Add a docs page in `docs/source/sources/<name>.rst` and add it to
   the `toctree` in `docs/source/sources/index.rst`.

The full guide is in `docs/source/contributing.rst`.

**Key constraints:**
- No `sqlalchemy` or `protea-core` imports. Plugins yield records only.
- Records are frozen pydantic models defined in `protea-contracts`.
- Parser tests run against canned bytes; HTTP tests use `requests` mocks.
  No DB or network access in CI.

---

## License

Released into the public domain under [The Unlicense](https://unlicense.org/).
See `LICENSE`.
