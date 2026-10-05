# Changelog

All notable changes to `protea-sources` are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
All four source plugins share a release cycle because adding a new
source typically means adding new typed records to `protea-contracts`,
whose version bump drives downstream rebuilds.

## [Unreleased]

### Added

- `UniProtSource.stream_release_fasta`: reads `UniProtProteinRecord`s
  from a UniProt release directory's gzipped flat files instead of
  walking the REST search endpoint by cursor. The two are
  content-equivalent for `reviewed:true` -- `uniprot_sprot.fasta.gz`
  plus `uniprot_sprot_varsplic.fasta.gz` are that query materialised --
  but they differ in cost. Measured on a 680,000-record fetch, cursor
  throughput decayed from 66 records/s over the first 50 pages to 17
  over the next 30, and to roughly 4 by page 80, which puts the
  wall-clock time of a full walk between 10 and 44 hours. The release
  files are static and served at full bandwidth: the same two files are
  102 MB and download in about 18 seconds. Each file's `file_done` event
  carries the md5 and byte count of the compressed bytes, matching what
  the release directory's `RELEASE.metalink` publishes, so the caller's
  log identifies the bytes a corpus came from and not merely the URL.
- `parse_fasta_lines`: the line-oriented form of `parse_fasta_text`,
  which now delegates to it. Lets a caller feed a decompressing stream
  straight in, so peak memory tracks one compressed file rather than
  the decompressed text.

- Optional raw-line filter on the GOA parser: `parse_gaf_line`,
  `parse_gaf_text` and `GoaSource.stream` accept an `accept` predicate
  (`RawLineFilter`) that runs on the tab-split columns *before*
  `GoaAnnotationRecord` is constructed. A caller loading a
  `goa_uniprot_all` release into a bounded protein universe keeps very
  little of it -- 671,138 records out of 280,922,738 lines on GOA 156,
  0.24% -- and previously paid for a validated record on every discarded
  line. Measured through the plugin on that release, 11.5 minutes becomes
  4.4 (2.6x), which is about 9 hours over the 75-release series. The
  predicate sees raw strings, so empty fields arrive as `""` rather than
  `None`; omitting it leaves every existing caller unchanged.
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
