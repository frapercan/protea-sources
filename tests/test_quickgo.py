"""Tests for the QuickGO source plugin.

Three layers (mirroring test_goa.py):

* Contract tests: plugin discoverable, ABC compliant, deprecated
  ``load`` raises with the right message.
* Parser tests: ``parse_quickgo_row`` + ``parse_quickgo_tsv`` +
  ``parse_eco_mapping`` against canned TSV / mapping fixtures.
* Stream wiring tests: ``QuickGoSource.stream`` and
  ``QuickGoSource.fetch_eco_mapping`` over mocked ``requests.get``.
"""

from __future__ import annotations

import io
from importlib.metadata import entry_points
from unittest.mock import MagicMock, patch

import pytest
from protea_contracts import (
    AnnotationSource,
    EcoMappingPayload,
    QuickGoAnnotationRecord,
    QuickGoStreamPayload,
)
from pydantic import ValidationError

from protea_sources.quickgo import (
    QuickGoSource,
    parse_eco_mapping,
    parse_quickgo_row,
    parse_quickgo_tsv,
    plugin,
)

# -- Contract tests -------------------------------------------------------


def test_plugin_is_quickgo_source_instance() -> None:
    assert isinstance(plugin, QuickGoSource)


def test_plugin_implements_annotation_source_abc() -> None:
    assert isinstance(plugin, AnnotationSource)


def test_plugin_name_is_quickgo() -> None:
    assert plugin.name == "quickgo"


def test_plugin_resolvable_via_entry_points() -> None:
    eps = entry_points(group="protea.sources")
    qg_eps = [ep for ep in eps if ep.name == "quickgo"]
    assert len(qg_eps) == 1
    resolved = qg_eps[0].load()
    assert resolved is plugin


# NOTE: ``load()`` was removed from the ABC in D-MIGR-06 (turn 37).
# Callers use ``QuickGoSource.stream`` and ``fetch_eco_mapping`` directly.


# -- Parser tests ---------------------------------------------------------


_HEADER = (
    "GENE PRODUCT ID\tGO TERM\tQUALIFIER\tECO ID\tREFERENCE\t"
    "WITH/FROM\tASSIGNED BY\tDATE"
)
_ROW = (
    "P12345\tGO:0000123\tinvolved_in\tECO:0000314\tPMID:99999\t"
    "UniProtKB:Q11111\tUniProtKB\t20240115"
)


class TestParseQuickGoRow:
    def test_full_row_maps_all_fields(self) -> None:
        header = _HEADER.split("\t")
        parts = _ROW.split("\t")
        rec = parse_quickgo_row(dict(zip(header, parts, strict=False)))
        assert rec == QuickGoAnnotationRecord(
            accession="P12345",
            go_id="GO:0000123",
            qualifier="involved_in",
            eco_id="ECO:0000314",
            db_reference="PMID:99999",
            with_from="UniProtKB:Q11111",
            assigned_by="UniProtKB",
            annotation_date="20240115",
        )

    def test_missing_accession_returns_none(self) -> None:
        assert parse_quickgo_row({"GO TERM": "GO:0000001"}) is None

    def test_missing_go_term_returns_none(self) -> None:
        assert parse_quickgo_row({"GENE PRODUCT ID": "P12345"}) is None

    def test_blank_accession_returns_none(self) -> None:
        assert parse_quickgo_row(
            {"GENE PRODUCT ID": "  ", "GO TERM": "GO:0000001"}
        ) is None

    def test_empty_optional_fields_become_none(self) -> None:
        rec = parse_quickgo_row(
            {
                "GENE PRODUCT ID": "P12345",
                "GO TERM": "GO:0000001",
                "QUALIFIER": "",
                "ECO ID": "  ",
                "REFERENCE": "",
            }
        )
        assert rec is not None
        assert rec.qualifier is None
        assert rec.eco_id is None
        assert rec.db_reference is None


class TestParseQuickGoTsv:
    def test_header_plus_rows(self) -> None:
        text = "\n".join([_HEADER, _ROW, "", _ROW.replace("P12345", "Q67890")])
        records = list(parse_quickgo_tsv(text))
        assert len(records) == 2
        assert records[0].accession == "P12345"
        assert records[1].accession == "Q67890"

    def test_short_rows_skipped(self) -> None:
        text = "\n".join([_HEADER, "P12345\tGO:0000123"])
        assert list(parse_quickgo_tsv(text)) == []

    def test_empty_text_yields_nothing(self) -> None:
        assert list(parse_quickgo_tsv("")) == []

    def test_only_header_yields_nothing(self) -> None:
        assert list(parse_quickgo_tsv(_HEADER)) == []


class TestParseEcoMapping:
    def test_valid_mapping(self) -> None:
        text = "ECO:0000314 IDA\nECO:0000256 IEA\nECO:0007005 IBA"
        mapping = parse_eco_mapping(text)
        assert mapping == {
            "ECO:0000314": "IDA",
            "ECO:0000256": "IEA",
            "ECO:0007005": "IBA",
        }

    def test_non_eco_lines_ignored(self) -> None:
        text = "# comment\n\nECO:0000314 IDA\nNOTAN:ECO XXX"
        assert parse_eco_mapping(text) == {"ECO:0000314": "IDA"}

    def test_short_lines_ignored(self) -> None:
        text = "ECO:0000314\nECO:0000256 IEA"
        assert parse_eco_mapping(text) == {"ECO:0000256": "IEA"}

    def test_empty_text(self) -> None:
        assert parse_eco_mapping("") == {}


# -- Stream wiring tests --------------------------------------------------


def _mock_response(body_bytes: bytes) -> MagicMock:
    raw = io.BytesIO(body_bytes)
    resp = MagicMock()
    resp.raw = raw
    resp.raise_for_status = MagicMock()
    return resp


def _capture_emit() -> tuple[object, list[tuple]]:
    captured: list[tuple] = []

    def emit(event: str, _payload: object, fields: object, level: str) -> None:
        captured.append((event, fields, level))

    return emit, captured


class TestStreamWiring:
    def test_unbatched_request_yields_records(self) -> None:
        # Includes an empty line and a short row to exercise the
        # parser's skip branches inside the streaming loop.
        body_text = "\n".join(
            [_HEADER, _ROW, "", "P99\tGO:0000001"]
        ) + "\n"
        body = body_text.encode("utf-8")
        payload = QuickGoStreamPayload(gene_product_ids=None)
        emit, _ = _capture_emit()
        with patch(
            "protea_sources.quickgo.requests.get",
            return_value=_mock_response(body),
        ) as mock_get:
            records = list(plugin.stream(payload, emit=emit))
        assert len(records) == 1
        assert records[0].accession == "P12345"
        # Single call, no geneProductId param.
        assert mock_get.call_count == 1
        params = mock_get.call_args.kwargs["params"]
        assert "geneProductId" not in params

    def test_batched_request_emits_batching_event(self) -> None:
        body = ("\n".join([_HEADER, _ROW]) + "\n").encode("utf-8")
        # 5 IDs, batch_size=2 → 3 batches (2,2,1).
        payload = QuickGoStreamPayload(
            gene_product_ids=["A", "B", "C", "D", "E"],
            gene_product_batch_size=2,
        )
        emit, captured = _capture_emit()
        # Each request needs a fresh response; reusing one closes the
        # underlying BytesIO after the first batch.
        with patch(
            "protea_sources.quickgo.requests.get",
            side_effect=lambda *_a, **_k: _mock_response(body),
        ) as mock_get:
            list(plugin.stream(payload, emit=emit))
        events = [event for event, *_ in captured]
        assert "source.quickgo.batching" in events
        assert events.count("source.quickgo.download_start") == 3
        assert mock_get.call_count == 3

    def test_progress_markers_present_per_batch(self) -> None:
        body = ("\n".join([_HEADER, _ROW]) + "\n").encode("utf-8")
        payload = QuickGoStreamPayload(
            gene_product_ids=["A", "B"], gene_product_batch_size=1
        )
        emit, captured = _capture_emit()
        with patch(
            "protea_sources.quickgo.requests.get",
            side_effect=lambda *_a, **_k: _mock_response(body),
        ):
            list(plugin.stream(payload, emit=emit))
        download_starts = [
            fields for event, fields, _ in captured if event == "source.quickgo.download_start"
        ]
        assert download_starts[0]["_progress_current"] == 1
        assert download_starts[0]["_progress_total"] == 2
        assert download_starts[1]["_progress_current"] == 2

    def test_http_error_propagates(self) -> None:
        payload = QuickGoStreamPayload()
        emit, _ = _capture_emit()
        resp = MagicMock()
        resp.raise_for_status.side_effect = RuntimeError("HTTP 500")
        with patch("protea_sources.quickgo.requests.get", return_value=resp):
            with pytest.raises(RuntimeError, match="HTTP 500"):
                list(plugin.stream(payload, emit=emit))

    def test_typed_timeout_passed_to_requests(self) -> None:
        body = b""
        payload = QuickGoStreamPayload(timeout_seconds=42)
        emit, _ = _capture_emit()
        with patch(
            "protea_sources.quickgo.requests.get",
            return_value=_mock_response(body),
        ) as mock_get:
            list(plugin.stream(payload, emit=emit))
        assert mock_get.call_args.kwargs["timeout"] == 42

    def test_request_includes_correct_user_agent_and_accept(self) -> None:
        body = b""
        payload = QuickGoStreamPayload()
        emit, _ = _capture_emit()
        with patch(
            "protea_sources.quickgo.requests.get",
            return_value=_mock_response(body),
        ) as mock_get:
            list(plugin.stream(payload, emit=emit))
        headers = mock_get.call_args.kwargs["headers"]
        assert headers["Accept"] == "text/tsv"
        assert "PROTEA" in headers["User-Agent"]


class TestFetchEcoMapping:
    def test_returns_parsed_mapping(self) -> None:
        text = "ECO:0000314 IDA\nECO:0000256 IEA"
        resp = MagicMock()
        resp.text = text
        resp.raise_for_status = MagicMock()
        emit, _ = _capture_emit()
        with patch("protea_sources.quickgo.requests.get", return_value=resp):
            mapping = plugin.fetch_eco_mapping(
                EcoMappingPayload(url="https://example.com/eco.txt"), emit=emit
            )
        assert mapping == {"ECO:0000314": "IDA", "ECO:0000256": "IEA"}

    def test_emits_start_and_done_events(self) -> None:
        resp = MagicMock()
        resp.text = ""
        resp.raise_for_status = MagicMock()
        emit, captured = _capture_emit()
        with patch("protea_sources.quickgo.requests.get", return_value=resp):
            plugin.fetch_eco_mapping(EcoMappingPayload(url="x"), emit=emit)
        events = [event for event, *_ in captured]
        assert "source.quickgo.eco_mapping_start" in events
        assert "source.quickgo.eco_mapping_done" in events

    def test_http_error_propagates(self) -> None:
        resp = MagicMock()
        resp.raise_for_status.side_effect = RuntimeError("HTTP 503")
        emit, _ = _capture_emit()
        with patch("protea_sources.quickgo.requests.get", return_value=resp):
            with pytest.raises(RuntimeError, match="HTTP 503"):
                plugin.fetch_eco_mapping(
                    EcoMappingPayload(url="x"), emit=emit
                )

    def test_typed_timeout_passed_to_requests(self) -> None:
        resp = MagicMock()
        resp.text = ""
        resp.raise_for_status = MagicMock()
        emit, _ = _capture_emit()
        with patch(
            "protea_sources.quickgo.requests.get", return_value=resp
        ) as mock_get:
            plugin.fetch_eco_mapping(
                EcoMappingPayload(url="x", timeout_seconds=15), emit=emit
            )
        assert mock_get.call_args.kwargs["timeout"] == 15


# -- Imports kept for ergonomics ------------------------------------------


def test_pydantic_validation_error_imported() -> None:
    """Anchor: rejected fields exercise pydantic strict mode."""
    with pytest.raises(ValidationError):
        QuickGoStreamPayload(gene_product_batch_size=-1)
