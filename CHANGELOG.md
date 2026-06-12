# Changelog

All notable changes to `protea-sources` are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
All four source plugins share a release cycle because adding a new
source typically means adding new typed records to `protea-contracts`,
whose version bump drives downstream rebuilds.

## [Unreleased]

### Added

- InterPro GO emission (`protea_sources.interpro.interpro2go`): the
  `interpro` plugin now turns InterProScan domain hits into
  `(protein, go_id, score)` GO predictions. Each hit's interpro2go GO
  terms (TSV column 14, captured into `InterProAnnotation.go_terms`) are
  asserted at a flat operating-point score and true-path-propagated up
  the ontology (`is_a` + `part_of`), with the maximum score winning per
  GO id. `InterProSource.predict_go` orchestrates the mapping;
  `load_obo_ancestors` builds the ancestor lookup from a GO OBO. The
  interpro2go release is pinned in `INTERPRO2GO_RELEASE` (overridable via
  `PROTEA_INTERPRO2GO_RELEASE`) and stamped onto every prediction.
- Sphinx documentation site expanded into a full guide: overview,
  quickstart, the annotation-source contract (including temporal-cutoff
  semantics), a GO and evidence-code mapping page, and an aggregated API
  reference. The build runs clean under `sphinx -W`.
- `docs` GitHub Actions workflow that builds the HTML with warnings
  treated as errors and uploads the result as an artifact.
- `iter_quickgo_records` helper exposing the shared QuickGO TSV
  header/row parsing contract.

### Changed

- Deduplicated the QuickGO TSV parsing loop: `parse_quickgo_tsv` and
  `QuickGoSource._fetch_page` now both delegate to
  `iter_quickgo_records`.
- Aligned the package version with the `v0.2.1` tag and trimmed the
  supported-Python metadata (classifiers, CI matrix) to `>=3.12` to
  match `pyproject` `requires-python`.
- Corrected README and UniProt documentation to match the live API
  (`stream_metadata`, `EcoMappingPayload`, `timeout_seconds`).

### Fixed

- CI matrix no longer attempts `poetry install` on Python 3.10 / 3.11,
  which cannot resolve the `>=3.12` dependency set.

## [0.2.1] - 2026-06-09

### Changed

- InterProScan runner routes output through a temporary file (`-o
  <tempfile>`) instead of stdout (`-o -`), which InterProScan 5.77
  silently drops, causing zero-annotation batches.

## [0.2.0]

### Added

- InterProScan source plugin (`interpro`): subprocess runner plus a
  pure-Python TSV parser, with binary resolution via payload override,
  `PROTEA_INTERPROSCAN_BIN`, or `$PATH`.
- UniProt source plugin (`uniprot`): FASTA + metadata TSV streaming with
  cursor pagination and a private retry/backoff HTTP client.

## [0.1.0]

### Added

- Initial release with the `goa` (UniProt-GOA bulk GAF) and `quickgo`
  (QuickGO REST API) source plugins, discovered via the `protea.sources`
  entry-points group.

[Unreleased]: https://github.com/frapercan/protea-sources/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/frapercan/protea-sources/releases/tag/v0.2.1
[0.2.0]: https://github.com/frapercan/protea-sources/releases/tag/v0.2.0
[0.1.0]: https://github.com/frapercan/protea-sources/releases/tag/v0.1.0
