"""Tests for the InterProScan source plugin (IP.1a + IP.1b).

Layers, mirroring ``test_goa.py`` / ``test_quickgo.py``:

* **Contract tests**: plugin is an :class:`AnnotationSource` instance,
  has the right ``name``/``version``, and is resolvable through the
  ``protea.sources`` entry-points group.
* **Parser tests**: ``parse_interproscan_tsv_line`` +
  ``parse_release_version_header`` + ``parse_interproscan_tsv``
  against canned TSV fixtures, including malformed and edge-case
  rows.
* **Adapter shim tests**: ``InterProSource.parse_tsv`` accepts both a
  filesystem path and an in-memory blob.
* **Runner tests (IP.1b)**: :meth:`InterProSource.run` invokes
  ``interproscan.sh`` via :func:`subprocess.run` (mocked), captures
  the ``--version`` banner, threads it onto every yielded record, and
  surfaces a clear :class:`RuntimeError` when the binary is missing.

The runner tests use :func:`unittest.mock.patch` so the CI runner does
not need a real InterProScan install.
"""

from __future__ import annotations

import subprocess
from importlib.metadata import entry_points
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from protea_contracts import AnnotationSource
from pydantic import ValidationError

from protea_sources.interpro import (
    ENV_BINARY_PATH,
    InterProAnnotation,
    InterProRunPayload,
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


# -- IP.1b: subprocess runner ---------------------------------------------


_VERSION_BANNER = "InterProScan-5.66-98.0\nCopyright (c) EMBL-EBI"
_RUN_TSV_BODY = _PFAM_ROW + "\n" + _GENE3D_ROW + "\n"


def _completed(stdout: str = "", stderr: str = "") -> MagicMock:
    """Build a stand-in :class:`subprocess.CompletedProcess`.

    A bare ``MagicMock`` is enough because the runner only reads
    ``.stdout`` / ``.stderr``; everything else stays untouched.
    """
    cp = MagicMock()
    cp.stdout = stdout
    cp.stderr = stderr
    cp.returncode = 0
    return cp


def _capture_emit() -> tuple[object, list[tuple]]:
    captured: list[tuple] = []

    def emit(event: str, _payload: object, fields: object, level: str) -> None:
        captured.append((event, fields, level))

    return emit, captured


def _make_subprocess_run_double(version_stdout: str, run_tsv: str) -> MagicMock:
    """Build a ``subprocess.run`` double that handles version and main run.

    The runner calls ``subprocess.run`` twice: once for ``--version``,
    once for the real invocation. For the real invocation the double
    locates the ``-o <path>`` argument and writes ``run_tsv`` to that
    file, mirroring what a real InterProScan 5.77 binary does (TSV goes
    to the named output file; stdout carries only log lines).
    Routing by argv keeps the test from coupling to call order.
    """

    def fake_run(argv: list[str], **_kwargs: object) -> MagicMock:
        if "--version" in argv:
            return _completed(stdout=version_stdout)
        # Locate -o <path> and write the TSV there (no stdout data rows).
        try:
            out_idx = argv.index("-o") + 1
            Path(argv[out_idx]).write_text(run_tsv, encoding="utf-8")
        except (ValueError, IndexError):
            pass
        return _completed(stdout="")

    return MagicMock(side_effect=fake_run)


class TestInterProRunPayload:
    def test_defaults_are_safe(self) -> None:
        payload = InterProRunPayload(fasta_path="/tmp/in.fa")
        assert payload.fasta_path == "/tmp/in.fa"
        assert payload.extra_args == []
        assert payload.timeout_seconds > 0
        assert payload.binary_path is None

    def test_frozen_rejects_mutation(self) -> None:
        payload = InterProRunPayload(fasta_path="/tmp/in.fa")
        with pytest.raises(ValidationError):
            payload.fasta_path = "/tmp/other.fa"  # type: ignore[misc]

    def test_extra_field_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            InterProRunPayload(
                fasta_path="/tmp/in.fa",
                unknown="boom",  # type: ignore[call-arg]
            )

    def test_zero_timeout_rejected(self) -> None:
        with pytest.raises(ValidationError):
            InterProRunPayload(fasta_path="/tmp/in.fa", timeout_seconds=0)


class TestInterProRunSubprocess:
    """Smoke tests for :meth:`InterProSource.run` with mocked subprocess."""

    def _run(self, payload: InterProRunPayload) -> tuple[list[InterProAnnotation], list[tuple]]:
        fake = _make_subprocess_run_double(_VERSION_BANNER, _RUN_TSV_BODY)
        emit, captured = _capture_emit()
        with patch("protea_sources.interpro.source.subprocess.run", fake):
            records = list(plugin.run(payload, emit=emit))
        return records, captured

    def test_yields_records_from_stdout_tsv(self) -> None:
        records, _ = self._run(InterProRunPayload(fasta_path="/tmp/in.fa"))
        assert len(records) == 2
        assert {r.source_db for r in records} == {"Pfam", "Gene3D"}

    def test_records_carry_version_banner(self) -> None:
        records, _ = self._run(InterProRunPayload(fasta_path="/tmp/in.fa"))
        for record in records:
            assert record.ipr_release_version == "InterProScan-5.66-98.0"

    def test_run_emits_start_and_done_events(self) -> None:
        _, captured = self._run(InterProRunPayload(fasta_path="/tmp/in.fa"))
        events = [event for event, *_ in captured]
        assert "source.interpro.run_start" in events
        assert "source.interpro.run_done" in events

    def test_version_event_carries_release_string(self) -> None:
        _, captured = self._run(InterProRunPayload(fasta_path="/tmp/in.fa"))
        starts = [fields for event, fields, _ in captured if event == "source.interpro.run_start"]
        assert starts
        assert starts[0]["release_version"] == "InterProScan-5.66-98.0"

    def test_argv_uses_canonical_flags(self) -> None:
        fake = _make_subprocess_run_double(_VERSION_BANNER, _RUN_TSV_BODY)
        emit, _ = _capture_emit()
        payload = InterProRunPayload(fasta_path="/data/in.fa")
        with patch("protea_sources.interpro.source.subprocess.run", fake):
            list(plugin.run(payload, emit=emit))
        # Second call is the real invocation; first is --version.
        argv_calls = [c.args[0] for c in fake.call_args_list]
        version_call = next(argv for argv in argv_calls if "--version" in argv)
        run_call = next(argv for argv in argv_calls if "--version" not in argv)
        assert version_call[0].endswith("interproscan.sh")
        # Canonical prefix: binary -i <fasta> -f tsv -o <real-file>.
        # The output path is a temp file (not the stdout-dash "-").
        assert run_call[:6] == [
            version_call[0],
            "-i",
            "/data/in.fa",
            "-f",
            "tsv",
            "-o",
        ]
        out_path = run_call[6]
        assert out_path != "-", (
            "InterProScan 5.77 does not stream TSV to stdout; "
            "-o must point to a real file, not '-'"
        )
        assert out_path.endswith(".tsv")

    def test_extra_args_appended_after_canonical_flags(self) -> None:
        fake = _make_subprocess_run_double(_VERSION_BANNER, _RUN_TSV_BODY)
        emit, _ = _capture_emit()
        payload = InterProRunPayload(
            fasta_path="/data/in.fa",
            extra_args=["-iprlookup", "-goterms", "-cpu", "4"],
        )
        with patch("protea_sources.interpro.source.subprocess.run", fake):
            list(plugin.run(payload, emit=emit))
        run_call = next(
            argv for argv in (c.args[0] for c in fake.call_args_list) if "--version" not in argv
        )
        assert run_call[-4:] == ["-iprlookup", "-goterms", "-cpu", "4"]

    def test_env_var_overrides_default_binary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_BINARY_PATH, "/opt/interproscan/interproscan.sh")
        fake = _make_subprocess_run_double(_VERSION_BANNER, _RUN_TSV_BODY)
        emit, _ = _capture_emit()
        payload = InterProRunPayload(fasta_path="/data/in.fa")
        with patch("protea_sources.interpro.source.subprocess.run", fake):
            list(plugin.run(payload, emit=emit))
        argv_calls = [call.args[0] for call in fake.call_args_list]
        assert all(argv[0] == "/opt/interproscan/interproscan.sh" for argv in argv_calls)

    def test_payload_binary_path_beats_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_BINARY_PATH, "/from/env/interproscan.sh")
        fake = _make_subprocess_run_double(_VERSION_BANNER, _RUN_TSV_BODY)
        emit, _ = _capture_emit()
        payload = InterProRunPayload(
            fasta_path="/data/in.fa",
            binary_path="/explicit/interproscan.sh",
        )
        with patch("protea_sources.interpro.source.subprocess.run", fake):
            list(plugin.run(payload, emit=emit))
        argv_calls = [call.args[0] for call in fake.call_args_list]
        assert all(argv[0] == "/explicit/interproscan.sh" for argv in argv_calls)

    def test_subprocess_run_uses_check_and_timeout(self) -> None:
        fake = _make_subprocess_run_double(_VERSION_BANNER, _RUN_TSV_BODY)
        emit, _ = _capture_emit()
        payload = InterProRunPayload(fasta_path="/data/in.fa", timeout_seconds=900)
        with patch("protea_sources.interpro.source.subprocess.run", fake):
            list(plugin.run(payload, emit=emit))
        for call in fake.call_args_list:
            assert call.kwargs.get("check") is True
            assert call.kwargs.get("timeout") is not None and call.kwargs["timeout"] > 0
        # Main-run call must use the payload-provided timeout.
        main_call = next(
            c for c in fake.call_args_list if "--version" not in c.args[0]
        )
        assert main_call.kwargs["timeout"] == 900

    def test_missing_binary_raises_runtime_error_with_hint(self) -> None:
        emit, _ = _capture_emit()
        payload = InterProRunPayload(fasta_path="/data/in.fa")
        with patch(
            "protea_sources.interpro.source.subprocess.run",
            side_effect=FileNotFoundError(2, "No such file", "interproscan.sh"),
        ):
            with pytest.raises(RuntimeError) as excinfo:
                list(plugin.run(payload, emit=emit))
        message = str(excinfo.value)
        assert "InterProScan" in message
        assert ENV_BINARY_PATH in message
        assert "interproscan.sh" in message

    def test_non_zero_exit_propagates_as_called_process_error(self) -> None:
        emit, _ = _capture_emit()
        payload = InterProRunPayload(fasta_path="/data/in.fa")
        # Version succeeds; the main run errors with a non-zero exit.
        version_double = _completed(stdout=_VERSION_BANNER)

        def fake_run(argv: list[str], **_kwargs: object) -> MagicMock:
            if "--version" in argv:
                return version_double
            raise subprocess.CalledProcessError(returncode=2, cmd=argv, stderr="boom")

        with patch("protea_sources.interpro.source.subprocess.run", side_effect=fake_run):
            with pytest.raises(subprocess.CalledProcessError):
                list(plugin.run(payload, emit=emit))

    def test_done_event_carries_tsv_bytes_not_stdout_bytes(self) -> None:
        """run_done event must use ``tsv_bytes`` key (not the old ``stdout_bytes``)."""
        _, captured = self._run(InterProRunPayload(fasta_path="/tmp/in.fa"))
        done_events = [(fields, level) for event, fields, level in captured
                       if event == "source.interpro.run_done"]
        assert done_events, "run_done event was not emitted"
        fields, _ = done_events[0]
        assert "tsv_bytes" in fields, (
            "run_done must carry tsv_bytes (file read), not stdout_bytes"
        )
        assert fields["tsv_bytes"] > 0


# -- Regression: stdout-vs-file capture (InterProScan 5.77) ---------------


class TestInterProFileCapture:
    """Regression tests for the stdout-vs-file capture bug.

    InterProScan 5.77 does NOT write TSV data rows to stdout when
    ``-o -`` is used.  The fix routes output through a named temp file
    (``-o <path>``).  These tests verify that:

    * a mock binary that writes TSV ONLY to the ``-o <file>`` path
      (and emits nothing on stdout) still produces parsed records;
    * a mock binary that emits TSV ONLY on stdout (the old broken path)
      would produce zero records (documenting the regression baseline).
    """

    @staticmethod
    def _run_with_file_writer(tsv_body: str) -> list[InterProAnnotation]:
        """Mock: writes TSV to the -o file, emits only log lines to stdout."""

        def fake_run(argv: list[str], **_kwargs: object) -> MagicMock:
            if "--version" in argv:
                return _completed(stdout=_VERSION_BANNER)
            # Write TSV to the -o path; emit only a log line on stdout.
            try:
                out_path = argv[argv.index("-o") + 1]
                Path(out_path).write_text(tsv_body, encoding="utf-8")
            except (ValueError, IndexError):
                pass
            return _completed(stdout="[INFO] InterProScan job finished.\n")

        emit, _ = _capture_emit()
        payload = InterProRunPayload(fasta_path="/data/in.fa")
        with patch("protea_sources.interpro.source.subprocess.run", MagicMock(side_effect=fake_run)):
            return list(plugin.run(payload, emit=emit))

    @staticmethod
    def _run_with_stdout_only(tsv_body: str) -> list[InterProAnnotation]:
        """Mock: writes TSV ONLY to stdout (the old broken behaviour)."""

        def fake_run(argv: list[str], **_kwargs: object) -> MagicMock:
            if "--version" in argv:
                return _completed(stdout=_VERSION_BANNER)
            # Intentionally do NOT write to the -o file.
            return _completed(stdout=tsv_body)

        emit, _ = _capture_emit()
        payload = InterProRunPayload(fasta_path="/data/in.fa")
        with patch("protea_sources.interpro.source.subprocess.run", MagicMock(side_effect=fake_run)):
            return list(plugin.run(payload, emit=emit))

    def test_file_writer_mock_yields_all_records(self) -> None:
        """Primary regression: output via -o file is fully parsed."""
        records = self._run_with_file_writer(_RUN_TSV_BODY)
        assert len(records) == 2, (
            f"Expected 2 records from file-based output; got {len(records)}. "
            "This is the core regression: -o <file> must be read, not stdout."
        )
        assert {r.source_db for r in records} == {"Pfam", "Gene3D"}

    def test_file_writer_records_carry_version(self) -> None:
        records = self._run_with_file_writer(_RUN_TSV_BODY)
        for record in records:
            assert record.ipr_release_version == "InterProScan-5.66-98.0"

    def test_stdout_only_mock_yields_zero_records(self) -> None:
        """Regression baseline: a mock that only emits stdout (no -o file)
        must produce 0 records, confirming the bug would return empty results
        if the old stdout-capture path were in place."""
        records = self._run_with_stdout_only(_RUN_TSV_BODY)
        assert len(records) == 0, (
            "Expected 0 records when TSV is only in stdout (not in the -o file). "
            "This documents the pre-fix baseline where every batch inserted 0 annotations."
        )
