"""Tests for the GOA source plugin.

Three layers:

* **Contract tests**: plugin discoverable via entry_points, ABC
  compliant, deprecated ``load`` raises with the right message.
* **Parser tests**: ``parse_gaf_line`` + ``parse_gaf_text`` against
  canned GAF fixtures. No HTTP, no DB, deterministic.
* **Stream wiring tests**: ``GoaSource.stream(...)`` over a mocked
  ``requests.get`` covering both plain-text and gzip code paths plus
  the start/done event emission.
"""

from __future__ import annotations

import gzip
import io
from importlib.metadata import entry_points
from unittest.mock import MagicMock, patch

import pytest
from protea_contracts import AnnotationSource, GoaAnnotationRecord, GoaStreamPayload
from pydantic import ValidationError

from protea_sources.goa import GoaSource, parse_gaf_line, parse_gaf_text, plugin

# -- Contract tests -------------------------------------------------------


def test_plugin_is_goa_source_instance() -> None:
    assert isinstance(plugin, GoaSource)


def test_plugin_implements_annotation_source_abc() -> None:
    assert isinstance(plugin, AnnotationSource)


def test_plugin_name_is_goa() -> None:
    assert plugin.name == "goa"


def test_plugin_resolvable_via_entry_points() -> None:
    eps = entry_points(group="protea.sources")
    goa_eps = [ep for ep in eps if ep.name == "goa"]
    assert len(goa_eps) == 1
    resolved = goa_eps[0].load()
    assert resolved is plugin


def test_load_is_deprecated_pending_d_migr_06() -> None:
    with pytest.raises(NotImplementedError, match=r"deprecated"):
        plugin.load(session=None, payload={}, emit=lambda *a, **k: None)


# -- Parser tests ---------------------------------------------------------


_VALID_LINE = (
    "UniProtKB\tP12345\tFOO\tinvolved_in\tGO:0000123\t"
    "PMID:99999\tIDA\tUniProtKB:Q11111\tF\tfoo\tgene1\tprotein\t"
    "taxon:9606\t20240115\tUniProtKB\tcontextX\textraX"
)


class TestParseGafLine:
    def test_valid_line_parses_to_record(self) -> None:
        record = parse_gaf_line(_VALID_LINE)
        assert record == GoaAnnotationRecord(
            accession="P12345",
            go_id="GO:0000123",
            qualifier="involved_in",
            evidence_code="IDA",
            db_reference="PMID:99999",
            with_from="UniProtKB:Q11111",
            assigned_by="UniProtKB",
            annotation_date="20240115",
        )

    def test_empty_line_returns_none(self) -> None:
        assert parse_gaf_line("") is None

    def test_comment_line_returns_none(self) -> None:
        assert parse_gaf_line("!gaf-version: 2.2") is None

    def test_short_line_returns_none(self) -> None:
        # 14 columns < the 15-column minimum.
        short = "\t".join(["x"] * 14)
        assert parse_gaf_line(short) is None

    def test_empty_optional_fields_become_none(self) -> None:
        line = (
            "UniProtKB\tP12345\tFOO\t\tGO:0000123\t"
            "\t\t\tF\tfoo\tgene1\tprotein\t"
            "taxon:9606\t\t\textraX"
        )
        record = parse_gaf_line(line)
        assert record is not None
        assert record.qualifier is None
        assert record.db_reference is None
        assert record.evidence_code is None
        assert record.with_from is None
        assert record.annotation_date is None
        assert record.assigned_by is None

    def test_record_is_immutable(self) -> None:
        record = parse_gaf_line(_VALID_LINE)
        assert record is not None
        with pytest.raises(ValidationError):
            record.accession = "X"  # type: ignore[misc]


class TestParseGafText:
    def test_mixed_text_yields_only_valid_records(self) -> None:
        text = "\n".join(
            [
                "!gaf-version: 2.2",
                "!comment line",
                "",
                _VALID_LINE,
                "short\tline",
                _VALID_LINE.replace("P12345", "Q67890"),
            ]
        )
        records = list(parse_gaf_text(text))
        assert len(records) == 2
        assert records[0].accession == "P12345"
        assert records[1].accession == "Q67890"

    def test_empty_text_yields_nothing(self) -> None:
        assert list(parse_gaf_text("")) == []

    def test_comments_only_yields_nothing(self) -> None:
        text = "!gaf-version: 2.2\n!hello\n!world"
        assert list(parse_gaf_text(text)) == []


# -- Stream wiring tests (HTTP + gzip) ------------------------------------


def _mock_response(body_bytes: bytes) -> MagicMock:
    """Build a minimal mock requests.Response that exposes ``raw`` as
    a BytesIO so the plugin's stream wrapper can iterate over it."""
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
    def test_plain_text_url_yields_records(self) -> None:
        body = (_VALID_LINE + "\n").encode("utf-8")
        payload = GoaStreamPayload(gaf_url="https://example.com/x.gaf")
        emit, _captured = _capture_emit()
        with patch(
            "protea_sources.goa.requests.get",
            return_value=_mock_response(body),
        ):
            records = list(plugin.stream(payload, emit=emit))
        assert len(records) == 1
        assert records[0].accession == "P12345"

    def test_gzip_url_decompresses_before_parsing(self) -> None:
        body = gzip.compress((_VALID_LINE + "\n").encode("utf-8"))
        payload = GoaStreamPayload(gaf_url="https://example.com/x.gaf.gz")
        emit, _captured = _capture_emit()
        with patch(
            "protea_sources.goa.requests.get",
            return_value=_mock_response(body),
        ):
            records = list(plugin.stream(payload, emit=emit))
        assert len(records) == 1
        assert records[0].accession == "P12345"

    def test_stream_emits_start_and_done_events(self) -> None:
        body = (_VALID_LINE + "\n").encode("utf-8")
        payload = GoaStreamPayload(gaf_url="https://example.com/x.gaf")
        emit, captured = _capture_emit()
        with patch(
            "protea_sources.goa.requests.get",
            return_value=_mock_response(body),
        ):
            list(plugin.stream(payload, emit=emit))
        events = [event for event, *_ in captured]
        assert "source.goa.download_start" in events
        assert "source.goa.download_done" in events

    def test_stream_propagates_http_errors(self) -> None:
        payload = GoaStreamPayload(gaf_url="https://example.com/x.gaf")
        emit, _captured = _capture_emit()
        resp = MagicMock()
        resp.raise_for_status.side_effect = RuntimeError("HTTP 500")
        with patch("protea_sources.goa.requests.get", return_value=resp):
            with pytest.raises(RuntimeError, match="HTTP 500"):
                list(plugin.stream(payload, emit=emit))

    def test_stream_passes_typed_timeout_to_requests(self) -> None:
        body = b""
        payload = GoaStreamPayload(
            gaf_url="https://example.com/x.gaf", timeout_seconds=42
        )
        emit, _captured = _capture_emit()
        with patch(
            "protea_sources.goa.requests.get",
            return_value=_mock_response(body),
        ) as mock_get:
            list(plugin.stream(payload, emit=emit))
        kwargs = mock_get.call_args.kwargs
        assert kwargs["timeout"] == 42
        assert kwargs["stream"] is True
