# protea-sources

Annotation source plugins for the PROTEA stack. Each sub-module
implements the `AnnotationSource` ABC from `protea-contracts` and
registers itself via `entry_points` group `protea.sources` so
`protea-core` discovers it at startup.

## Sub-modules

| Sub-module | Source | Status |
|------------|--------|--------|
| `protea_sources.goa` | UniProt-GOA bulk download (EBI FTP) | F2A.6 (currently placeholder) |
| `protea_sources.quickgo` | QuickGO REST API | F2A.6 (currently placeholder) |
| `protea_sources.uniprot` | UniProt REST (FASTA + metadata) | F2A.6 (currently placeholder) |
| `protea_sources.interproscan` | InterProScan local runs | future, post-defensa |

## How extensibility works

Adding a new source is a single sub-module here plus an entry in
`pyproject.toml`:

```toml
[tool.poetry.plugins."protea.sources"]
my_source = "protea_sources.my_source:plugin"
```

`protea-core` starts up, queries `entry_points(group="protea.sources")`,
and registers each plugin in the operation registry. No platform
code changes.

## Roadmap

This is the F0 bootstrap (T0.12 of the PROTEA master plan v3).
The current `load_goa_annotations`, `load_quickgo_annotations`,
`insert_proteins`, `fetch_uniprot_metadata` operations in
`protea-core` migrate here in F2A.6.

## Versioning

SemVer 2.0.0; per-source release independence is achievable
later (F9 post-defensa) by splitting each sub-module into its
own repo. Today they share a release cycle.

## Development

```bash
poetry install
poetry run pytest
poetry run ruff check .
poetry run mypy src tests
```
