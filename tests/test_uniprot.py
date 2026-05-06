"""Tests for the UniProt source plugin.

Layers (mirroring test_goa.py / test_quickgo.py):

* Contract tests: plugin discoverable, ABC compliant, deprecated
  ``load`` raises with the right message, ``stream`` raises with
  guidance toward ``stream_fasta``.
* Parser tests: ``parse_fasta_header`` + ``parse_fasta_text`` against
  canned FASTA fixtures (canonical, isoform, multi-line sequence,
  unreviewed ``tr|`` entries, broken headers).
* HTTP helper tests: ``extract_next_cursor`` against real Link
  headers; ``UniProtRetryClient`` against mocked requests for 429,
  5xx, retry-after, and network errors.
* Stream wiring tests: ``UniProtSource.stream_fasta`` over a mocked
  session covering plain text, gzip, multi-page cursor pagination,
  HTTP counters, event emission.
"""

from __future__ import annotations

import gzip
from importlib.metadata import entry_points
from unittest.mock import MagicMock, patch

import pytest
from protea_contracts import (
    AnnotationSource,
    UniProtFastaStreamPayload,
    UniProtMetadataRecord,
    UniProtMetadataStreamPayload,
    UniProtProteinRecord,
)

from protea_sources.uniprot import (
    UniProtSource,
    parse_fasta_header,
    parse_fasta_text,
    parse_metadata_tsv,
    plugin,
)
from protea_sources.uniprot._http import UniProtRetryClient, extract_next_cursor

# -- Contract tests -------------------------------------------------------


def test_plugin_is_uniprot_source_instance() -> None:
    assert isinstance(plugin, UniProtSource)


def test_plugin_implements_annotation_source_abc() -> None:
    assert isinstance(plugin, AnnotationSource)


def test_plugin_name_is_uniprot() -> None:
    assert plugin.name == "uniprot"


def test_plugin_resolvable_via_entry_points() -> None:
    eps = entry_points(group="protea.sources")
    up_eps = [ep for ep in eps if ep.name == "uniprot"]
    assert len(up_eps) == 1
    resolved = up_eps[0].load()
    assert resolved is plugin


def test_load_is_deprecated_pending_d_migr_06() -> None:
    with pytest.raises(NotImplementedError, match=r"deprecated"):
        plugin.load(session=None, payload={}, emit=lambda *a, **k: None)


def test_stream_redirects_to_specific_methods() -> None:
    with pytest.raises(NotImplementedError, match=r"stream_metadata"):
        list(plugin.stream({}, emit=lambda *a, **k: None))


# -- Parser tests ---------------------------------------------------------


_HEADER_REVIEWED = (
    "sp|P12345|FOO_HUMAN Foo protein OS=Homo sapiens OX=9606 GN=FOO PE=1 SV=2"
)
_HEADER_UNREVIEWED = (
    "tr|Q67890|Q67890_MOUSE Putative bar OS=Mus musculus OX=10090 GN=Bar PE=4 SV=1"
)
_HEADER_ISOFORM = (
    "sp|P12345-2|FOO_HUMAN Isoform 2 OS=Homo sapiens OX=9606 GN=FOO PE=1 SV=2"
)


class TestParseFastaHeader:
    def test_reviewed_canonical(self) -> None:
        parsed = parse_fasta_header(_HEADER_REVIEWED)
        assert parsed["accession"] == "P12345"
        assert parsed["entry_name"] == "FOO_HUMAN"
        assert parsed["canonical_accession"] == "P12345"
        assert parsed["is_canonical"] is True
        assert parsed["isoform_index"] is None
        assert parsed["reviewed"] is True
        assert parsed["organism"] == "Homo sapiens"
        assert parsed["taxonomy_id"] == "9606"
        assert parsed["gene_name"] == "FOO"

    def test_unreviewed(self) -> None:
        parsed = parse_fasta_header(_HEADER_UNREVIEWED)
        assert parsed["reviewed"] is False
        assert parsed["accession"] == "Q67890"

    def test_isoform_splits_canonical(self) -> None:
        parsed = parse_fasta_header(_HEADER_ISOFORM)
        assert parsed["accession"] == "P12345-2"
        assert parsed["canonical_accession"] == "P12345"
        assert parsed["is_canonical"] is False
        assert parsed["isoform_index"] == 2

    def test_header_without_pipes(self) -> None:
        parsed = parse_fasta_header("FREEFORM_ID Foo")
        assert parsed["accession"] == "FREEFORM_ID"
        assert parsed["entry_name"] is None
        assert parsed["reviewed"] is False

    def test_missing_organism_markers(self) -> None:
        parsed = parse_fasta_header("sp|P12345|FOO_HUMAN")
        assert parsed["accession"] == "P12345"
        assert parsed["organism"] is None
        assert parsed["taxonomy_id"] is None
        assert parsed["gene_name"] is None


class TestParseFastaText:
    def test_single_record(self) -> None:
        fasta = f">{_HEADER_REVIEWED}\nMKTAYIAK\n"
        records = list(parse_fasta_text(fasta))
        assert len(records) == 1
        assert records[0].accession == "P12345"
        assert records[0].sequence == "MKTAYIAK"
        assert records[0].length == 8
        assert len(records[0].sequence_hash) == 32

    def test_two_records(self) -> None:
        fasta = (
            f">{_HEADER_REVIEWED}\nMKTAYIAK\n"
            f">{_HEADER_UNREVIEWED}\nACDEFGHIK\n"
        )
        records = list(parse_fasta_text(fasta))
        assert len(records) == 2
        assert records[0].reviewed is True
        assert records[1].reviewed is False

    def test_multiline_sequence(self) -> None:
        fasta = f">{_HEADER_REVIEWED}\nMKTA\nYIAK\nACDE\n"
        records = list(parse_fasta_text(fasta))
        assert records[0].sequence == "MKTAYIAKACDE"
        assert records[0].length == 12

    def test_whitespace_in_sequence_stripped(self) -> None:
        fasta = f">{_HEADER_REVIEWED}\n  MKTA YIAK  \n"
        records = list(parse_fasta_text(fasta))
        assert records[0].sequence == "MKTAYIAK"

    def test_empty_sequence_skipped(self) -> None:
        fasta = (
            f">{_HEADER_REVIEWED}\n\n>{_HEADER_UNREVIEWED}\nACDE\n"
        )
        records = list(parse_fasta_text(fasta))
        assert len(records) == 1
        assert records[0].accession == "Q67890"

    def test_isoform_record_carries_index(self) -> None:
        fasta = f">{_HEADER_ISOFORM}\nMKTA\n"
        records = list(parse_fasta_text(fasta))
        assert records[0].isoform_index == 2

    def test_empty_text_yields_nothing(self) -> None:
        assert list(parse_fasta_text("")) == []

    def test_record_is_uniprot_protein_record_instance(self) -> None:
        fasta = f">{_HEADER_REVIEWED}\nMKTAYIAK\n"
        records = list(parse_fasta_text(fasta))
        assert isinstance(records[0], UniProtProteinRecord)


# -- HTTP helper tests ----------------------------------------------------


class TestExtractNextCursor:
    def test_extracts_cursor(self) -> None:
        link = '<https://example.com?cursor=abc123>; rel="next"'
        assert extract_next_cursor(link) == "abc123"

    def test_no_next_returns_none(self) -> None:
        link = '<https://example.com?cursor=abc>; rel="prev"'
        assert extract_next_cursor(link) is None

    def test_empty_header_returns_none(self) -> None:
        assert extract_next_cursor("") is None

    def test_no_cursor_param_returns_none(self) -> None:
        link = '<https://example.com?page=2>; rel="next"'
        assert extract_next_cursor(link) is None


class TestUniProtRetryClient:
    def _payload(self, **overrides) -> UniProtFastaStreamPayload:
        defaults = {
            "search_criteria": "x",
            "max_retries": 3,
            "backoff_base_seconds": 0.0,
            "backoff_max_seconds": 0.0,
            "jitter_seconds": 0.0,
        }
        defaults.update(overrides)
        return UniProtFastaStreamPayload(**defaults)

    def test_success_returns_response(self) -> None:
        client = UniProtRetryClient()
        resp = MagicMock(status_code=200)
        with patch.object(client.session, "get", return_value=resp):
            assert client.get_with_retries("u", self._payload(), lambda *a, **k: None) is resp
        assert client.requests == 1
        assert client.retries == 0

    def test_retries_on_429(self) -> None:
        client = UniProtRetryClient()
        bad = MagicMock(status_code=429, headers={})
        good = MagicMock(status_code=200)
        with patch.object(client.session, "get", side_effect=[bad, good]):
            client.get_with_retries("u", self._payload(), lambda *a, **k: None)
        assert client.requests == 2
        assert client.retries == 1

    def test_honors_retry_after(self) -> None:
        client = UniProtRetryClient()
        bad = MagicMock(status_code=429, headers={"Retry-After": "0"})
        good = MagicMock(status_code=200)
        events: list[tuple] = []

        def emit(event, _p, fields, _l):
            events.append((event, fields))

        with patch.object(client.session, "get", side_effect=[bad, good]):
            client.get_with_retries("u", self._payload(), emit)
        retry_events = [e for e in events if e[0] == "http.retry"]
        assert any(e[1].get("reason") == "retry_after" for e in retry_events)

    def test_max_retries_exhausted_raises(self) -> None:
        client = UniProtRetryClient()
        bad = MagicMock(status_code=503, headers={})
        bad.raise_for_status.side_effect = RuntimeError("503")
        with patch.object(client.session, "get", return_value=bad):
            with pytest.raises(RuntimeError, match="503"):
                client.get_with_retries("u", self._payload(max_retries=1), lambda *a, **k: None)

    def test_request_exception_retries(self) -> None:
        import requests as _r

        client = UniProtRetryClient()
        good = MagicMock(status_code=200)
        with patch.object(
            client.session, "get",
            side_effect=[_r.ConnectionError("boom"), good],
        ):
            client.get_with_retries("u", self._payload(), lambda *a, **k: None)
        assert client.retries == 1


# -- Stream wiring tests --------------------------------------------------


def _mock_resp(body_bytes: bytes, link_header: str = "", status: int = 200) -> MagicMock:
    resp = MagicMock(status_code=status)
    resp.content = body_bytes
    resp.headers = {"link": link_header}
    resp.raise_for_status = MagicMock()
    return resp


def _capture_emit() -> tuple[object, list[tuple]]:
    captured: list[tuple] = []

    def emit(event, _p, fields, _l):
        captured.append((event, fields))

    return emit, captured


_FASTA_TWO = (
    f">{_HEADER_REVIEWED}\nMKTA\n"
    f">{_HEADER_UNREVIEWED}\nACDE\n"
).encode()


class TestStreamFastaWiring:
    def _payload(self) -> UniProtFastaStreamPayload:
        return UniProtFastaStreamPayload(
            search_criteria="reviewed:true",
            max_retries=2,
            backoff_base_seconds=0.0,
            backoff_max_seconds=0.0,
            jitter_seconds=0.0,
        )

    def test_single_page_yields_records(self) -> None:
        plugin_instance = UniProtSource()
        emit, _ = _capture_emit()
        with patch.object(
            plugin_instance._client.session,
            "get",
            return_value=_mock_resp(_FASTA_TWO),
        ):
            records = list(plugin_instance.stream_fasta(self._payload(), emit=emit))
        assert len(records) == 2
        assert records[0].accession == "P12345"
        assert records[1].accession == "Q67890"

    def test_multi_page_pagination_via_cursor(self) -> None:
        plugin_instance = UniProtSource()
        emit, _ = _capture_emit()
        page1 = _mock_resp(
            _FASTA_TWO,
            link_header='<https://example.com?cursor=PAGE2>; rel="next"',
        )
        page2 = _mock_resp(
            f">{_HEADER_ISOFORM}\nMKTAYIAK\n".encode(),
            link_header="",  # no next, terminate
        )
        with patch.object(
            plugin_instance._client.session,
            "get",
            side_effect=[page1, page2],
        ) as mock_get:
            records = list(plugin_instance.stream_fasta(self._payload(), emit=emit))
        assert len(records) == 3
        assert mock_get.call_count == 2
        # Second call URL must carry the cursor.
        second_url = mock_get.call_args_list[1].args[0]
        assert "cursor=PAGE2" in second_url

    def test_gzip_compressed_response_decompresses(self) -> None:
        plugin_instance = UniProtSource()
        emit, _ = _capture_emit()
        body = gzip.compress(_FASTA_TWO)
        payload = UniProtFastaStreamPayload(
            search_criteria="x",
            compressed=True,
            max_retries=2,
            backoff_base_seconds=0.0,
            backoff_max_seconds=0.0,
            jitter_seconds=0.0,
        )
        with patch.object(
            plugin_instance._client.session,
            "get",
            return_value=_mock_resp(body),
        ):
            records = list(plugin_instance.stream_fasta(payload, emit=emit))
        assert len(records) == 2

    def test_query_includes_isoforms_flag(self) -> None:
        plugin_instance = UniProtSource()
        emit, _ = _capture_emit()
        with patch.object(
            plugin_instance._client.session,
            "get",
            return_value=_mock_resp(b""),
        ) as mock_get:
            list(plugin_instance.stream_fasta(self._payload(), emit=emit))
        url = mock_get.call_args.args[0]
        assert "includeIsoform=true" in url
        assert "format=fasta" in url

    def test_progress_events_emitted(self) -> None:
        plugin_instance = UniProtSource()
        emit, captured = _capture_emit()
        with patch.object(
            plugin_instance._client.session,
            "get",
            return_value=_mock_resp(_FASTA_TWO),
        ):
            list(plugin_instance.stream_fasta(self._payload(), emit=emit))
        events = [e for e, *_ in captured]
        assert "source.uniprot_fasta.start" in events
        assert "source.uniprot_fasta.fetch_page_start" in events
        assert "source.uniprot_fasta.fetch_page_done" in events

    def test_http_counters_after_run(self) -> None:
        plugin_instance = UniProtSource()
        emit, _ = _capture_emit()
        with patch.object(
            plugin_instance._client.session,
            "get",
            return_value=_mock_resp(_FASTA_TWO),
        ):
            list(plugin_instance.stream_fasta(self._payload(), emit=emit))
        requests, retries = plugin_instance.http_counters
        assert requests == 1
        assert retries == 0

    def test_counters_reset_between_runs(self) -> None:
        plugin_instance = UniProtSource()
        emit, _ = _capture_emit()
        bad = MagicMock(status_code=500, headers={})
        good = _mock_resp(_FASTA_TWO)
        with patch.object(
            plugin_instance._client.session,
            "get",
            side_effect=[bad, good],
        ):
            list(plugin_instance.stream_fasta(self._payload(), emit=emit))
        assert plugin_instance.http_counters == (2, 1)
        # Second run with no retries should reset counters.
        with patch.object(
            plugin_instance._client.session,
            "get",
            return_value=_mock_resp(b""),
        ):
            list(plugin_instance.stream_fasta(self._payload(), emit=emit))
        assert plugin_instance.http_counters == (1, 0)


# -- Metadata parser + stream tests ---------------------------------------


_TSV_HEADER = (
    "Entry\tReviewed\tEntry Name\tProtein names\tGene Names\tOrganism\t"
    "Length\tActive site\tEC number\tFunction [CC]\tKeywords"
)
_TSV_ROW_REVIEWED = (
    "P12345\treviewed\tFOO_HUMAN\tFoo protein\tFOO\tHomo sapiens\t"
    "350\tACT_SITE 100\t1.1.1.1\tCatalytic role.\tEnzyme;Hydrolase"
)
_TSV_ROW_UNREVIEWED = (
    "Q67890\tunreviewed\tQ67890_MOUSE\tPutative bar\tBar\tMus musculus\t"
    "210\t\t\t\t"
)


class TestParseMetadataTsv:
    def test_two_rows_yield_two_records(self) -> None:
        text = "\n".join([_TSV_HEADER, _TSV_ROW_REVIEWED, _TSV_ROW_UNREVIEWED])
        records = list(parse_metadata_tsv(text))
        assert len(records) == 2
        assert records[0].accession == "P12345"
        assert records[0].raw_fields["Active site"] == "ACT_SITE 100"
        assert records[1].accession == "Q67890"
        assert records[1].raw_fields["Active site"] == ""

    def test_empty_entry_row_skipped(self) -> None:
        text = "\n".join([
            _TSV_HEADER,
            "\treviewed\tNAME\t\t\t\t\t\t\t\t",  # blank Entry
            _TSV_ROW_REVIEWED,
        ])
        records = list(parse_metadata_tsv(text))
        assert len(records) == 1
        assert records[0].accession == "P12345"

    def test_header_only_yields_nothing(self) -> None:
        assert list(parse_metadata_tsv(_TSV_HEADER)) == []

    def test_empty_text_yields_nothing(self) -> None:
        assert list(parse_metadata_tsv("")) == []

    def test_records_are_uniprot_metadata_record_instances(self) -> None:
        text = "\n".join([_TSV_HEADER, _TSV_ROW_REVIEWED])
        records = list(parse_metadata_tsv(text))
        assert isinstance(records[0], UniProtMetadataRecord)


class TestStreamMetadataWiring:
    def _payload(self, **overrides) -> UniProtMetadataStreamPayload:
        defaults = {
            "search_criteria": "reviewed:true",
            "fields": ["accession", "protein_name"],
            "max_retries": 2,
            "backoff_base_seconds": 0.0,
            "backoff_max_seconds": 0.0,
            "jitter_seconds": 0.0,
        }
        defaults.update(overrides)
        return UniProtMetadataStreamPayload(**defaults)

    def test_single_page_yields_records(self) -> None:
        plugin_instance = UniProtSource()
        emit, _ = _capture_emit()
        body = ("\n".join([_TSV_HEADER, _TSV_ROW_REVIEWED]) + "\n").encode()
        with patch.object(
            plugin_instance._client.session,
            "get",
            return_value=_mock_resp(body),
        ):
            records = list(
                plugin_instance.stream_metadata(self._payload(compressed=False), emit=emit)
            )
        assert len(records) == 1
        assert records[0].accession == "P12345"

    def test_gzip_compressed_response_decompresses(self) -> None:
        plugin_instance = UniProtSource()
        emit, _ = _capture_emit()
        body = gzip.compress(("\n".join([_TSV_HEADER, _TSV_ROW_REVIEWED]) + "\n").encode())
        with patch.object(
            plugin_instance._client.session,
            "get",
            return_value=_mock_resp(body),
        ):
            records = list(plugin_instance.stream_metadata(self._payload(), emit=emit))
        assert len(records) == 1

    def test_query_includes_fields_param(self) -> None:
        plugin_instance = UniProtSource()
        emit, _ = _capture_emit()
        with patch.object(
            plugin_instance._client.session,
            "get",
            return_value=_mock_resp(b""),
        ) as mock_get:
            list(plugin_instance.stream_metadata(
                self._payload(fields=["accession", "ft_act_site"], compressed=False),
                emit=emit,
            ))
        url = mock_get.call_args.args[0]
        assert "format=tsv" in url
        assert "fields=accession" in url
        assert "ft_act_site" in url

    def test_compressed_default_true_in_url(self) -> None:
        plugin_instance = UniProtSource()
        emit, _ = _capture_emit()
        body = gzip.compress(_TSV_HEADER.encode() + b"\n")
        with patch.object(
            plugin_instance._client.session,
            "get",
            return_value=_mock_resp(body),
        ) as mock_get:
            list(plugin_instance.stream_metadata(self._payload(), emit=emit))
        url = mock_get.call_args.args[0]
        assert "compressed=true" in url

    def test_cursor_pagination(self) -> None:
        plugin_instance = UniProtSource()
        emit, _ = _capture_emit()
        body1 = ("\n".join([_TSV_HEADER, _TSV_ROW_REVIEWED]) + "\n").encode()
        body2 = ("\n".join([_TSV_HEADER, _TSV_ROW_UNREVIEWED]) + "\n").encode()
        page1 = _mock_resp(
            body1, link_header='<https://example.com?cursor=PAGE2>; rel="next"'
        )
        page2 = _mock_resp(body2, link_header="")
        with patch.object(
            plugin_instance._client.session,
            "get",
            side_effect=[page1, page2],
        ) as mock_get:
            records = list(
                plugin_instance.stream_metadata(self._payload(compressed=False), emit=emit)
            )
        assert len(records) == 2
        assert mock_get.call_count == 2
        assert "cursor=PAGE2" in mock_get.call_args_list[1].args[0]

    def test_progress_events_emitted(self) -> None:
        plugin_instance = UniProtSource()
        emit, captured = _capture_emit()
        body = ("\n".join([_TSV_HEADER, _TSV_ROW_REVIEWED]) + "\n").encode()
        with patch.object(
            plugin_instance._client.session,
            "get",
            return_value=_mock_resp(body),
        ):
            list(plugin_instance.stream_metadata(self._payload(compressed=False), emit=emit))
        events = [e for e, *_ in captured]
        assert "source.uniprot_metadata.start" in events
        assert "source.uniprot_metadata.fetch_page_start" in events
        assert "source.uniprot_metadata.fetch_page_done" in events
