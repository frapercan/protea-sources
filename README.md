# protea-sources

Annotation source plugins for the [PROTEA](https://github.com/frapercan/protea)
stack. Each sub-module implements the `AnnotationSource` marker ABC
from [`protea-contracts`](https://github.com/frapercan/protea-contracts)
and registers via the `protea.sources` `entry_points` group, so
`protea-core` discovers it at startup.

The sources are **self-contained**: HTTP retries / pagination / parsing
all live here. Persistence (DB writes against PROTEA's ORM) stays in
the calling operation. The plugin/operation boundary is defined by
the typed records and payloads in `protea-contracts.records`.

## 5 minutes to your first parsed record

```bash
pip install protea-sources
```

```python
from protea_contracts import GoaStreamPayload
from protea_sources.goa import plugin as goa

# Stream parsed records from a GAF URL.
emit = lambda *a, **k: None  # ignore structured-event callbacks
payload = GoaStreamPayload(gaf_url="https://example.com/small.gaf")

for record in goa.stream(payload, emit=emit):
    print(record.accession, record.go_id, record.evidence_code)
    # P12345 GO:0000123 IDA
```

The same shape works for QuickGO (TSV with optional ECO mapping)
and UniProt FASTA / metadata (cursor-paginated):

```python
from protea_contracts import (
    QuickGoStreamPayload, UniProtFastaStreamPayload,
    UniProtMetadataStreamPayload,
)
from protea_sources.quickgo import plugin as quickgo
from protea_sources.uniprot import plugin as uniprot

# QuickGO with auxiliary ECO mapping fetch (D-MIGR-05 of master plan v3).
eco_map = quickgo.fetch_eco_mapping(...)
for record in quickgo.stream(QuickGoStreamPayload(...), emit=emit):
    evidence_code = eco_map.get(record.eco_id, record.eco_id)

# UniProt has two modalities — call the specific method.
for protein in uniprot.stream_fasta(UniProtFastaStreamPayload(...), emit=emit):
    print(protein.accession, protein.canonical_accession, protein.length)

for meta in uniprot.stream_metadata(UniProtMetadataStreamPayload(...), emit=emit):
    print(meta.accession, meta.raw_fields["Active site"])
```

Records are frozen pydantic models defined in
`protea-contracts.records` — typos fail at construction, dataflow is
one-way, drift is impossible.

## Sources shipped today

| Sub-module | Source | Modality | Status |
|------------|--------|----------|--------|
| `protea_sources.goa` | UniProt-GOA bulk download (EBI FTP) | GAF stream | **active** (turn 25 of master plan v3) |
| `protea_sources.quickgo` | QuickGO REST API | TSV + ECO mapping | **active** (turn 27) |
| `protea_sources.uniprot` | UniProt REST | FASTA + metadata TSV | **active** (turns 32, 34) |
| `protea_sources.interproscan` | InterProScan local runs | future | post-defensa |

All three active sources have **100% test coverage** on the parsing
+ HTTP wiring. The `_http.py` retry helper (UniProt-only today)
absorbs the legacy `UniProtHttpMixin` that used to live in PROTEA;
shared retry/backoff/jitter behaviour with Retry-After honouring.

## Why a separate package

1. **Plugin extensibility.** New sources are added without touching
   `protea-core`. A single sub-module here plus one entry in
   `pyproject.toml`.
2. **Self-contained.** HTTP, parsing, and source-specific retries
   live here. `protea-core` owns persistence; the boundary is the
   typed record stream defined in `protea-contracts`.
3. **Testable in isolation.** No DB, no SQLAlchemy. Parser tests run
   against canned bytes; HTTP tests run against `requests.get` mocks.

## Adding a new source

1. Create `src/protea_sources/<your_name>/__init__.py`.
2. Subclass `AnnotationSource` (marker ABC). Set `name = "<your_name>"`
   and `version = "<release-id>"`.
3. Define the modality method(s). For most sources a single
   `stream(payload, *, emit) -> Iterator[YourRecord]` is enough;
   UniProt is an exception with two modalities.
4. Add typed payload + record models in `protea-contracts.records`
   (or here if the source is private to your fork).
5. Register the plugin under `[tool.poetry.plugins."protea.sources"]`
   in `pyproject.toml`.
6. Mirror the existing test files (`tests/test_<your_name>.py`) with
   contract tests, parser tests, and stream-wiring tests.

The full guide lives in the Sphinx docs under
`docs/source/contributing.rst`.

## Versioning

SemVer 2.0.0; per-source release independence is deferred to F9
(post-defensa) by splitting each sub-module into its own repo.
Today they share a release cycle because adding a new source
typically means adding new typed records to `protea-contracts` too,
and the version bump there is what drives downstream re-builds.

## Development

```bash
poetry install
poetry run pytest             # ~175 tests, ~0.3s
poetry run ruff check .
poetry run mypy --strict src
```

## Documentation

Full Sphinx documentation in `docs/source/`. Build locally with
`poetry install --with docs && cd docs && make html`. Each source
has its own page documenting the modality, the URL shape, the TSV
column mapping (where applicable), and the record fields exposed.

## License

MIT. See `LICENSE`.
