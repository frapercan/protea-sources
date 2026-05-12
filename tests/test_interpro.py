"""Tests for the InterProScan source plugin (IP.1a — scaffold + parser).

Three layers, mirroring ``test_goa.py`` / ``test_quickgo.py``:

* **Contract tests**: plugin is an :class:`AnnotationSource` instance,
  has the right ``name``/``version``, and is resolvable through the
  ``protea.sources`` entry-points group.
* **Parser tests**: ``parse_interproscan_tsv_line`` +
  ``parse_release_version_header`` + ``parse_interproscan_tsv``
  against canned TSV fixtures, including malformed and edge-case
  rows.
* **Adapter shim tests**: ``InterProSource.parse_tsv`` accepts both a
  filesystem path and an in-memory blob.

No HTTP, no subprocess, no DB — IP.1a is parse-only. IP.1b will add
``interproscan.sh`` invocation tests under a separate fixture.
"""

from __future__ import annotations

from importlib.metadata import entry_points
from pathlib import Path

import pytest
from protea_contracts import AnnotationSource
from pydantic import ValidationError

from protea_sources.interpro import (
    InterProAnnotation,
    InterProSource,
    parse_interproscan_tsv,
    parse_interproscan_tsv_line,
    parse_release_version_header,
    plugin,
)

# -- Fixtures -------------------------------------------------------------

# Realistic InterProScan TSV row (Pfam analysis, integrated InterPro
# entry). Columns 13-15 (description / GO / pathways) are present but
# the parser ignores them in IP.1a — keep them so the fixture mirrors
# what an actual run emits.
_PFAM_ROW = (
    "P51587\t"  # 1  protein accession
    "abc123md5\t"  # 2  md5
    "3418\t"  # 3  sequence length
    "Pfam\t"  # 4  analysis (source DB)
    "PF09103\t"  # 5  signature accession
    "BRCA2, oligonucleotide/oligosaccharide-binding\t"  # 6  signature description
    "2670\t"  # 7  start
    "2799\t"  # 8  end
    "7.9E-43\t"  # 9  score / e-value
    "T\t"  # 10 status
    "08-04-2024\t"  # 11 date
    "IPR015187\t"  # 12 InterPro accession
    "BRCA2, OB1\t"  # 13 InterPro description
    "GO:0006281\t"  # 14 GO terms
    "-"  # 15 pathways
)

_GENE3D_ROW = (
    "P51587\t"
    "abc123md5\t"
    "3418\t"
    "Gene3D\t"
    "G3DSA:2.40.50.140\t"
    "Nucleic acid-binding proteins\t"
    "2200\t"
    "2300\t"
    "1.2E-10\t"
    "T\t"
    "08-04-2024\t"
    "-\t"  # no integrated InterPro entry — uses literal "-"
    "-\t"
    "-\t"
    "-"
)

_VERSION_HEADER = "# InterProScan-5.66-98.0"


# -- Contract tests -------------------------------------------------------


def test_plugin_is_interpro_source_instance() -> None:
    assert isinstance(plugin, InterProSource)


def test_plugin_implements_annotation_source_abc() -> None:
    assert isinstance(plugin, AnnotationSource)


def test_plugin_name_is_interpro() -> None:
    assert plugin.name == "interpro"


def test_plugin_version_is_string() -> None:
    assert isinstance(plugin.version, str)
    assert plugin.version


def test_plugin_resolvable_via_entry_points() -> None:
    eps = entry_points(group="protea.sources")
    matches = [ep for ep in eps if ep.name == "interpro"]
    assert len(matches) == 1
    resolved = matches[0].load()
    assert resolved is plugin


# -- Header parser --------------------------------------------------------


class TestParseReleaseVersionHeader:
    def test_canonical_form(self) -> None:
        assert parse_release_version_header("# InterProScan-5.66-98.0") == "InterProScan-5.66-98.0"

    def test_no_space_after_hash(self) -> None:
        assert parse_release_version_header("#InterProScan-5.66-98.0") == "InterProScan-5.66-98.0"

    def test_non_comment_line_returns_none(self) -> None:
        assert parse_release_version_header("P51587\tabc\t3418") is None

    def test_blank_comment_returns_none(self) -> None:
        assert parse_release_version_header("#") is None
        assert parse_release_version_header("#   ") is None


# -- Line parser ----------------------------------------------------------


class TestParseInterproscanTsvLine:
    def test_pfam_row_maps_all_fields(self) -> None:
        record = parse_interproscan_tsv_line(_PFAM_ROW, release_version="InterProScan-5.66-98.0")
        assert record == InterProAnnotation(
            source_db="Pfam",
            accession="P51587",
            start=2670,
            end=2799,
            evidence="7.9E-43",
            ipr_version="IPR015187",
            ipr_release_version="InterProScan-5.66-98.0",
        )

    def test_dash_in_ipr_accession_becomes_none(self) -> None:
        record = parse_interproscan_tsv_line(_GENE3D_ROW)
        assert record is not None
        assert record.ipr_version is None
        # Release version not threaded -> None on this call.
        assert record.ipr_release_version is None

    def test_release_version_threaded_through(self) -> None:
        record = parse_interproscan_tsv_line(_GENE3D_ROW, release_version="InterProScan-5.66-98.0")
        assert record is not None
        assert record.ipr_release_version == "InterProScan-5.66-98.0"

    def test_empty_line_returns_none(self) -> None:
        assert parse_interproscan_tsv_line("") is None
        assert parse_interproscan_tsv_line("   \n") is None

    def test_comment_line_returns_none(self) -> None:
        assert parse_interproscan_tsv_line(_VERSION_HEADER) is None

    def test_short_row_returns_none(self) -> None:
        # 10 columns < the 11-column minimum.
        short = "\t".join(["x"] * 10)
        assert parse_interproscan_tsv_line(short) is None

    def test_minimum_11_columns_accepted(self) -> None:
        # Exactly 11 cells — no IPR mapping column at all.
        cells = [
            "P00000",  # accession
            "md5x",
            "100",
            "Pfam",
            "PF00001",
            "Hit description",
            "10",
            "50",
            "1e-5",
            "T",
            "01-01-2024",
        ]
        record = parse_interproscan_tsv_line("\t".join(cells))
        assert record is not None
        assert record.accession == "P00000"
        assert record.ipr_version is None

    def test_non_integer_start_returns_none(self) -> None:
        bad = _PFAM_ROW.replace("\t2670\t", "\tNOPE\t")
        assert parse_interproscan_tsv_line(bad) is None

    def test_start_greater_than_end_returns_none(self) -> None:
        bad = _PFAM_ROW.replace("\t2670\t2799\t", "\t9000\t100\t")
        assert parse_interproscan_tsv_line(bad) is None

    def test_record_is_immutable(self) -> None:
        record = parse_interproscan_tsv_line(_PFAM_ROW)
        assert record is not None
        with pytest.raises(ValidationError):
            record.accession = "X"  # type: ignore[misc]


# -- Bulk parser ----------------------------------------------------------


class TestParseInterproscanTsvBlob:
    def test_mixed_blob_yields_only_valid_records(self) -> None:
        blob = "\n".join(
            [
                _VERSION_HEADER,
                "",
                _PFAM_ROW,
                "short\trow",
                _GENE3D_ROW,
                _PFAM_ROW.replace("P51587", "Q67890"),
            ]
        )
        records = list(parse_interproscan_tsv(blob))
        assert len(records) == 3
        accessions = [r.accession for r in records]
        assert accessions == ["P51587", "P51587", "Q67890"]
        # All three records picked up the release tag from the header.
        for record in records:
            assert record.ipr_release_version == "InterProScan-5.66-98.0"

    def test_no_header_leaves_release_version_none(self) -> None:
        blob = _PFAM_ROW
        records = list(parse_interproscan_tsv(blob))
        assert len(records) == 1
        assert records[0].ipr_release_version is None

    def test_empty_blob_yields_nothing(self) -> None:
        assert list(parse_interproscan_tsv("")) == []

    def test_iterable_of_lines_supported(self) -> None:
        lines = [_VERSION_HEADER, _PFAM_ROW, _GENE3D_ROW]
        records = list(parse_interproscan_tsv(lines))
        assert len(records) == 2

    def test_path_to_file_supported(self, tmp_path: Path) -> None:
        target = tmp_path / "interpro.tsv"
        target.write_text(
            "\n".join([_VERSION_HEADER, _PFAM_ROW, _GENE3D_ROW]),
            encoding="utf-8",
        )
        records = list(parse_interproscan_tsv(target))
        assert len(records) == 2
        assert records[0].source_db == "Pfam"
        assert records[1].source_db == "Gene3D"

    def test_missing_path_with_no_newlines_raises(self, tmp_path: Path) -> None:
        # A non-existent path-shaped string with no newline content
        # would otherwise be silently treated as an empty blob; the
        # parser raises FileNotFoundError for Path inputs so the
        # caller learns about the typo.
        ghost = tmp_path / "does_not_exist.tsv"
        with pytest.raises(FileNotFoundError):
            list(parse_interproscan_tsv(ghost))


# -- Adapter shim on the plugin class -------------------------------------


class TestPluginParseTsv:
    def test_parses_blob_through_plugin(self) -> None:
        blob = "\n".join([_VERSION_HEADER, _PFAM_ROW])
        records = list(plugin.parse_tsv(blob))
        assert len(records) == 1
        assert records[0].source_db == "Pfam"
        assert records[0].ipr_release_version == "InterProScan-5.66-98.0"

    def test_parses_file_through_plugin(self, tmp_path: Path) -> None:
        target = tmp_path / "interpro.tsv"
        target.write_text(_PFAM_ROW, encoding="utf-8")
        records = list(plugin.parse_tsv(target))
        assert len(records) == 1
        assert records[0].accession == "P51587"


# -- Pydantic strict-mode anchor ------------------------------------------


def test_pydantic_rejects_zero_start() -> None:
    with pytest.raises(ValidationError):
        InterProAnnotation(
            source_db="Pfam",
            accession="P00001",
            start=0,
            end=100,
        )


def test_pydantic_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        InterProAnnotation(
            source_db="Pfam",
            accession="P00001",
            start=10,
            end=100,
            unknown_field="boom",  # type: ignore[call-arg]
        )
