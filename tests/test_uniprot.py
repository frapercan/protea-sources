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
import hashlib
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
    ACCESSIONS_URL,
    MAX_ACCESSIONS_PER_REQUEST,
    MAX_OR_CONDITIONS,
    MAX_UNIPARC_OR_CONDITIONS,
    SEARCH_URL,
    UNIPARC_SEARCH_URL,
    RetryKnobs,
    UniProtSource,
    parse_fasta_header,
    parse_fasta_lines,
    parse_fasta_text,
    parse_metadata_tsv,
    parse_uniparc_tsv,
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


# NOTE: ``load()`` was removed from the ABC in D-MIGR-06 (turn 37).
# Callers use ``stream_fasta`` and ``stream_metadata`` directly.


def test_stream_redirects_to_specific_methods() -> None:
    with pytest.raises(NotImplementedError, match=r"stream_metadata"):
        list(plugin.stream({}, emit=lambda *a, **k: None))


# -- Parser tests ---------------------------------------------------------


_HEADER_REVIEWED = "sp|P12345|FOO_HUMAN Foo protein OS=Homo sapiens OX=9606 GN=FOO PE=1 SV=2"
_HEADER_UNREVIEWED = "tr|Q67890|Q67890_MOUSE Putative bar OS=Mus musculus OX=10090 GN=Bar PE=4 SV=1"
_HEADER_ISOFORM = "sp|P12345-2|FOO_HUMAN Isoform 2 OS=Homo sapiens OX=9606 GN=FOO PE=1 SV=2"


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
        fasta = f">{_HEADER_REVIEWED}\nMKTAYIAK\n>{_HEADER_UNREVIEWED}\nACDEFGHIK\n"
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
        fasta = f">{_HEADER_REVIEWED}\n\n>{_HEADER_UNREVIEWED}\nACDE\n"
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
            client.session,
            "get",
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


_FASTA_TWO = (f">{_HEADER_REVIEWED}\nMKTA\n>{_HEADER_UNREVIEWED}\nACDE\n").encode()


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
    "Q67890\tunreviewed\tQ67890_MOUSE\tPutative bar\tBar\tMus musculus\t210\t\t\t\t"
)


class TestParseFastaLines:
    """The line-oriented parser is what the release path consumes."""

    def test_accepts_an_iterator_not_just_a_list(self) -> None:
        # A decompressing stream is an iterator with no len() and no
        # second pass; the parser must not quietly need a sequence.
        lines = iter(_FASTA_TWO.decode().splitlines())
        records = list(parse_fasta_lines(lines))
        assert [r.accession for r in records] == ["P12345", "Q67890"]

    def test_does_not_buffer_the_whole_input(self) -> None:
        # Pull one record and assert the source was not drained: this
        # is the property that keeps peak memory off the file size.
        consumed: list[str] = []

        def counting_lines():
            for line in _FASTA_TWO.decode().splitlines():
                consumed.append(line)
                yield line

        stream = parse_fasta_lines(counting_lines())
        first = next(stream)
        assert first.accession == "P12345"
        # Reaching record 1 requires seeing record 2's header, and no more.
        assert len(consumed) == 3

    def test_text_wrapper_agrees_with_the_string_form(self) -> None:
        from io import BytesIO, TextIOWrapper

        via_text = list(parse_fasta_text(_FASTA_TWO.decode()))
        via_lines = list(parse_fasta_lines(TextIOWrapper(BytesIO(_FASTA_TWO))))
        assert [r.accession for r in via_text] == [r.accession for r in via_lines]
        assert [r.sequence_hash for r in via_text] == [r.sequence_hash for r in via_lines]


class TestStreamReleaseFastaWiring:
    def _payload(self) -> UniProtFastaStreamPayload:
        return UniProtFastaStreamPayload(
            search_criteria="reviewed:true",
            max_retries=2,
            backoff_base_seconds=0.0,
            backoff_max_seconds=0.0,
            jitter_seconds=0.0,
        )

    def test_gunzips_and_yields_records(self) -> None:
        plugin_instance = UniProtSource()
        emit, _ = _capture_emit()
        with patch.object(
            plugin_instance._client.session,
            "get",
            return_value=_mock_resp(gzip.compress(_FASTA_TWO)),
        ):
            records = list(
                plugin_instance.stream_release_fasta(
                    ["https://example.com/uniprot_sprot.fasta.gz"],
                    payload=self._payload(),
                    emit=emit,
                )
            )
        assert [r.accession for r in records] == ["P12345", "Q67890"]

    def test_concatenates_the_files_in_the_order_given(self) -> None:
        # Canonical file then varsplic: isoforms must arrive after their
        # canonical entries, which is the order the operation relies on
        # when it groups rows by canonical_accession.
        plugin_instance = UniProtSource()
        emit, _ = _capture_emit()
        varsplic = f">{_HEADER_ISOFORM}\nMKTAYIAK\n".encode()
        canonical_url = "https://example.com/uniprot_sprot.fasta.gz"
        varsplic_url = "https://example.com/uniprot_sprot_varsplic.fasta.gz"
        # Dispatch on the URL, not on call order: a side_effect list
        # would answer in sequence whatever was asked for, and would
        # still pass if the implementation walked the URLs backwards.
        bodies = {
            canonical_url: gzip.compress(_FASTA_TWO),
            varsplic_url: gzip.compress(varsplic),
        }
        with patch.object(
            plugin_instance._client.session,
            "get",
            side_effect=lambda url, **_kw: _mock_resp(bodies[url]),
        ) as mock_get:
            records = list(
                plugin_instance.stream_release_fasta(
                    [canonical_url, varsplic_url],
                    payload=self._payload(),
                    emit=emit,
                )
            )
        assert mock_get.call_count == 2
        assert [r.accession for r in records] == ["P12345", "Q67890", "P12345-2"]
        assert records[2].canonical_accession == "P12345"
        assert records[2].isoform_index == 2

    def test_requests_exactly_the_urls_given(self) -> None:
        # No cursor, no query string: the release path must not rewrite
        # or paginate the URLs it was handed.
        plugin_instance = UniProtSource()
        emit, _ = _capture_emit()
        url = "https://example.com/release/uniprot_sprot.fasta.gz"
        with patch.object(
            plugin_instance._client.session,
            "get",
            return_value=_mock_resp(gzip.compress(_FASTA_TWO)),
        ) as mock_get:
            list(plugin_instance.stream_release_fasta([url], payload=self._payload(), emit=emit))
        assert mock_get.call_count == 1
        assert mock_get.call_args_list[0].args[0] == url

    def test_emits_a_per_file_record_count(self) -> None:
        plugin_instance = UniProtSource()
        emit, captured = _capture_emit()
        body = gzip.compress(_FASTA_TWO)
        with patch.object(plugin_instance._client.session, "get", return_value=_mock_resp(body)):
            list(
                plugin_instance.stream_release_fasta(
                    ["https://example.com/uniprot_sprot.fasta.gz"],
                    payload=self._payload(),
                    emit=emit,
                )
            )
        done = [f for e, f in captured if e == "source.uniprot_release_fasta.file_done"]
        assert done == [
            {
                "file": 1,
                "records": 2,
                "md5": hashlib.md5(body, usedforsecurity=False).hexdigest(),
                "bytes": len(body),
            }
        ]

    def test_emits_the_md5_of_the_compressed_bytes(self) -> None:
        # The digest must be of the .gz as served, which is what a
        # release directory's RELEASE.metalink publishes -- not of the
        # decompressed FASTA, which no checksum is published for.
        plugin_instance = UniProtSource()
        emit, captured = _capture_emit()
        body = gzip.compress(_FASTA_TWO)
        with patch.object(plugin_instance._client.session, "get", return_value=_mock_resp(body)):
            list(
                plugin_instance.stream_release_fasta(
                    ["https://example.com/uniprot_sprot.fasta.gz"],
                    payload=self._payload(),
                    emit=emit,
                )
            )
        (done,) = [f for e, f in captured if e == "source.uniprot_release_fasta.file_done"]
        assert done["md5"] == hashlib.md5(body, usedforsecurity=False).hexdigest()
        assert done["md5"] != hashlib.md5(_FASTA_TWO, usedforsecurity=False).hexdigest()

    def test_counts_http_requests_for_the_operation(self) -> None:
        plugin_instance = UniProtSource()
        emit, _ = _capture_emit()
        with patch.object(
            plugin_instance._client.session,
            "get",
            side_effect=[
                _mock_resp(gzip.compress(_FASTA_TWO)),
                _mock_resp(gzip.compress(_FASTA_TWO)),
            ],
        ):
            list(
                plugin_instance.stream_release_fasta(
                    ["https://example.com/a.gz", "https://example.com/b.gz"],
                    payload=self._payload(),
                    emit=emit,
                )
            )
        requests_made, retries = plugin_instance.http_counters
        assert requests_made == 2
        assert retries == 0


class TestTheOneShotFetches:
    """The two calls that let an operation stop opening its own socket.

    They exist because ``ensure_goa_universe`` grew three UniProt endpoints and
    a second copy of the retry client inside PROTEA, undoing a migration this
    plugin's own docstring describes as finished.
    """

    def _knobs(self) -> RetryKnobs:
        return RetryKnobs(
            max_retries=2, backoff_base_seconds=0.0, backoff_max_seconds=0.0, jitter_seconds=0.0
        )

    def test_batch_fetch_asks_for_exactly_the_accessions_given(self) -> None:
        p = UniProtSource()
        emit, _ = _capture_emit()
        with patch.object(
            p._client.session, "get", return_value=_mock_resp(b"acc\tseq\nP12345\tMKT\n")
        ) as mock_get:
            salida = p.fetch_accessions_tsv(
                ["P12345", "Q67890"], fields="accession,sequence", emit=emit, knobs=self._knobs()
            )
        url = mock_get.call_args_list[0].args[0]
        assert url.startswith(ACCESSIONS_URL)
        assert "accessions=P12345,Q67890" in url
        assert "fields=accession,sequence" in url
        assert "format=tsv" in url
        assert salida == "acc\tseq\nP12345\tMKT\n"

    def test_batch_fetch_refuses_more_than_uniprot_allows(self) -> None:
        # Nombrado aqui en vez de dejarlo al 400 de UniProt, para que el mensaje
        # apunte al troceado del llamante y no al servicio.
        p = UniProtSource()
        emit, _ = _capture_emit()
        with (
            patch.object(p._client.session, "get") as mock_get,
            pytest.raises(ValueError, match="chunk before calling"),
        ):
            p.fetch_accessions_tsv(
                ["P12345"] * (MAX_ACCESSIONS_PER_REQUEST + 1),
                fields="accession",
                emit=emit,
            )
        assert not mock_get.called

    def test_a_shorter_answer_is_not_an_error(self) -> None:
        # Una accesion que UniProt ya no sirve se OMITE del cuerpo. Que la
        # respuesta traiga menos filas que accesiones pedidas significa "ya no
        # esta", no "fallo", y por eso no se levanta nada aqui.
        p = UniProtSource()
        emit, _ = _capture_emit()
        with patch.object(
            p._client.session, "get", return_value=_mock_resp(b"acc\tseq\nP12345\tMKT\n")
        ):
            salida = p.fetch_accessions_tsv(
                ["P12345", "A0A024QYT6"],
                fields="accession,sequence",
                emit=emit,
                knobs=self._knobs(),
            )
        assert "A0A024QYT6" not in salida
        assert salida.count("\n") == 2

    def test_secondary_search_builds_a_sec_acc_query(self) -> None:
        p = UniProtSource()
        emit, _ = _capture_emit()
        with patch.object(
            p._client.session,
            "get",
            return_value=_mock_resp(b'{"results": [{"primaryAccession": "P9WEV8"}]}'),
        ) as mock_get:
            salida = p.search_secondary_accessions(["C8VQ65"], emit=emit, knobs=self._knobs())
        url = mock_get.call_args_list[0].args[0]
        assert url.startswith(SEARCH_URL)
        assert "sec_acc%3AC8VQ65" in url or "sec_acc:C8VQ65" in url
        # Sin fields: pedir fields=sec_acc da 400, es campo de CONSULTA y no de
        # retorno, asi que el mapa se lee del documento completo.
        assert "fields=" not in url
        assert salida == {"results": [{"primaryAccession": "P9WEV8"}]}

    def test_secondary_search_refuses_more_than_the_or_cap(self) -> None:
        p = UniProtSource()
        emit, _ = _capture_emit()
        with (
            patch.object(p._client.session, "get") as mock_get,
            pytest.raises(ValueError, match="chunk before calling"),
        ):
            p.search_secondary_accessions(["C8VQ65"] * (MAX_OR_CONDITIONS + 1), emit=emit)
        assert not mock_get.called

    def test_both_reuse_the_existing_retry_client(self) -> None:
        # Un 503 del Varnish de UniProt no puede tirar la llamada: el cliente que
        # ya existia reintenta 429 y 5xx, y por eso no hacia falta otro.
        for llamada in (
            lambda p, e: p.fetch_accessions_tsv(
                ["P12345"], fields="accession", emit=e, knobs=self._knobs()
            ),
            lambda p, e: p.search_secondary_accessions(["C8VQ65"], emit=e, knobs=self._knobs()),
        ):
            p = UniProtSource()
            emit, _ = _capture_emit()
            with (
                patch.object(
                    p._client.session,
                    "get",
                    side_effect=[_mock_resp(b"", status=503), _mock_resp(b'{"results": []}')],
                ) as mock_get,
                patch("time.sleep"),
            ):
                llamada(p, emit)
            assert mock_get.call_count == 2
            assert p.http_counters == (2, 1)

    def test_exhausted_retries_raise_and_never_return_empty(self) -> None:
        # EL INVARIANTE: un lote que fallo no es un lote que no encontro nada.
        # Devolver "" aqui se contaria como cero resultados, que es el defecto
        # que ya costo una medicion: diez lotes dieron 400 y los fallos se
        # tallaron como ceros, leyendose como "0% recuperable".
        #
        # El doble levanta en raise_for_status como hace la convencion de
        # TestUniProtRetryClient, porque un MagicMock no lo hace solo: sin eso
        # este test pasaba por StopIteration del side_effect y no probaba nada.
        p = UniProtSource()
        emit, _ = _capture_emit()
        mala = _mock_resp(b"", status=503)
        mala.raise_for_status.side_effect = RuntimeError("503 agotado")
        with (
            patch.object(p._client.session, "get", return_value=mala),
            patch("time.sleep"),
            pytest.raises(RuntimeError, match="503 agotado"),
        ):
            p.fetch_accessions_tsv(["P12345"], fields="accession", emit=emit, knobs=self._knobs())

    def test_the_service_limits_are_facts_not_tunables(self) -> None:
        # Medidos contra el servicio: 1001 accesiones responden "Only '1000'
        # accessions are allowed in each request", y 101 condiciones OR
        # responden "Maximum allowed is 100".
        assert MAX_ACCESSIONS_PER_REQUEST == 1000
        assert MAX_OR_CONDITIONS == 100

    def test_knobs_have_defaults_so_a_caller_need_not_care(self) -> None:
        k = RetryKnobs()
        assert k.max_retries >= 1 and k.timeout_seconds > 0
        assert k.backoff_base_seconds > 0 and k.backoff_max_seconds >= k.backoff_base_seconds


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
        text = "\n".join(
            [
                _TSV_HEADER,
                "\treviewed\tNAME\t\t\t\t\t\t\t\t",  # blank Entry
                _TSV_ROW_REVIEWED,
            ]
        )
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
            list(
                plugin_instance.stream_metadata(
                    self._payload(fields=["accession", "ft_act_site"], compressed=False),
                    emit=emit,
                )
            )
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
        page1 = _mock_resp(body1, link_header='<https://example.com?cursor=PAGE2>; rel="next"')
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


# -- UniParc, the route for what UniProtKB stopped serving ----------------

#: A real answer, trimmed. The first row is the shape that makes the caller's
#: job a decision: ONE sequence shared by four accessions, only one of which
#: was asked for, and three of them carrying a version suffix while the fourth
#: does not.
_UNIPARC_HEADER = "Entry\tUniProtKB\tFirst seen\tLast seen\tLength\tSequence"
_UNIPARC_SHARED = (
    "UPI0001FE0681\tA0A014NF26.1; E9F541.1; A0A0B4FY72.1; A0A7D5Z9H9\t"
    "2011-03-01\t2026-09-02\t53\tMKTSLALVLGAAASIVTAGIVITPIKDDQIVPRMGDDCAFGVVTPQGCGPKRN"
)
_UNIPARC_SINGLE = "UPI00045646E2\tA0A023GU64.1\t2014-05-07\t2024-07-17\t1059\tMKTSLALVLG"


class TestParseUniparcTsv:
    """The parser hands back every row and decides nothing."""

    def test_a_row_shared_by_four_accessions_keeps_all_four(self) -> None:
        rows = list(parse_uniparc_tsv(_UNIPARC_HEADER + "\n" + _UNIPARC_SHARED + "\n"))
        assert len(rows) == 1
        assert rows[0].accessions == (
            "A0A014NF26.1",
            "E9F541.1",
            "A0A0B4FY72.1",
            "A0A7D5Z9H9",
        ), (
            "identical sequences share a UPI, so a row answers for accessions "
            "nobody asked about; dropping them here would hide that from the "
            "caller, who is the one that has to intersect with its own batch"
        )

    def test_the_version_suffix_is_passed_through_untouched(self) -> None:
        rows = list(parse_uniparc_tsv(_UNIPARC_HEADER + "\n" + _UNIPARC_SHARED + "\n"))
        assert "A0A014NF26.1" in rows[0].accessions
        assert "A0A7D5Z9H9" in rows[0].accessions, (
            "both forms occur in one cell; normalising them here would be this "
            "module deciding that the version carries no information"
        )

    def test_the_dates_survive_because_they_are_how_a_version_is_chosen(self) -> None:
        rows = list(parse_uniparc_tsv(_UNIPARC_HEADER + "\n" + _UNIPARC_SINGLE + "\n"))
        assert (rows[0].first_seen, rows[0].last_seen) == ("2014-05-07", "2024-07-17")

    def test_several_rows_for_one_accession_all_come_back(self) -> None:
        dos = _UNIPARC_HEADER + "\n" + _UNIPARC_SINGLE + "\n" + _UNIPARC_SHARED + "\n"
        assert len(list(parse_uniparc_tsv(dos))) == 2, (
            "one accession can answer several rows, one per sequence version; "
            "picking one is the caller's decision and needs them all"
        )

    def test_a_row_without_a_sequence_is_skipped(self) -> None:
        sin = "UPI0000000001\tQ00001.1\t2011-03-01\t2026-09-02\t0\t"
        rows = list(parse_uniparc_tsv(_UNIPARC_HEADER + "\n" + sin + "\n"))
        assert rows == [], "a UniParc row with no sequence is the one thing this route is for"

    def test_the_header_drives_the_mapping_so_field_order_is_free(self) -> None:
        otro = "Sequence\tEntry\tUniProtKB\nMKTS\tUPI0000000002\tQ00002.1\n"
        rows = list(parse_uniparc_tsv(otro))
        assert rows[0].upi == "UPI0000000002" and rows[0].sequence == "MKTS"
        assert rows[0].length is None, "a column the caller did not ask for is absent, not zero"

    def test_header_only_and_empty_text_yield_nothing(self) -> None:
        assert list(parse_uniparc_tsv(_UNIPARC_HEADER + "\n")) == []
        assert list(parse_uniparc_tsv("")) == []


class TestSearchUniparcTsv:
    def test_the_cap_fires_before_the_request_not_after(self) -> None:
        plugin_instance = UniProtSource()
        emit, _ = _capture_emit()
        with patch.object(plugin_instance._client.session, "get") as llamada:
            with pytest.raises(ValueError, match="exceeds UniParc's limit"):
                plugin_instance.search_uniparc_tsv(
                    [f"Q{i:05d}" for i in range(MAX_UNIPARC_OR_CONDITIONS + 1)],
                    fields="upi,accession,sequence",
                    emit=emit,
                )
        assert not llamada.called, (
            "UniParc answers too many conditions with 200 and an empty body, so "
            "letting the request through would read as 'none of these exist' and "
            "record every accession in the batch as unrecoverable"
        )

    def test_the_cap_is_lower_than_the_uniprotkb_one(self) -> None:
        assert MAX_UNIPARC_OR_CONDITIONS < MAX_OR_CONDITIONS, (
            "the two endpoints do not share a limit; reusing MAX_OR_CONDITIONS "
            "here is the mistake this constant exists to prevent"
        )

    def test_it_asks_uniparc_and_carries_the_fields(self) -> None:
        plugin_instance = UniProtSource()
        emit, _ = _capture_emit()
        with patch.object(
            plugin_instance._client.session,
            "get",
            return_value=_mock_resp((_UNIPARC_HEADER + "\n" + _UNIPARC_SINGLE).encode()),
        ) as llamada:
            cuerpo = plugin_instance.search_uniparc_tsv(
                ["A0A023GU64"],
                fields="upi,accession,first_seen,last_seen,length,sequence",
                emit=emit,
            )
        url = llamada.call_args[0][0]
        assert url.startswith(UNIPARC_SEARCH_URL)
        assert "format=tsv" in url
        assert "first_seen" in url and "sequence" in url
        assert "A0A023GU64" in url
        assert "UPI00045646E2" in cuerpo

    def test_the_batch_is_joined_with_or(self) -> None:
        plugin_instance = UniProtSource()
        emit, _ = _capture_emit()
        with patch.object(
            plugin_instance._client.session,
            "get",
            return_value=_mock_resp(_UNIPARC_HEADER.encode()),
        ) as llamada:
            plugin_instance.search_uniparc_tsv(
                ["Q00001", "Q00002"], fields="upi,sequence", emit=emit
            )
        url = llamada.call_args[0][0]
        assert "Q00001" in url and "Q00002" in url and "OR" in url

    def test_it_announces_the_batch_it_is_about_to_ask_for(self) -> None:
        plugin_instance = UniProtSource()
        emit, captured = _capture_emit()
        with patch.object(
            plugin_instance._client.session,
            "get",
            return_value=_mock_resp(_UNIPARC_HEADER.encode()),
        ):
            plugin_instance.search_uniparc_tsv(
                ["Q00001", "Q00002"], fields="upi,sequence", emit=emit
            )
        eventos = [e for e, _ in captured]
        assert "source.uniparc.search_start" in eventos
        campos = next(f for e, f in captured if e == "source.uniparc.search_start")
        assert campos["accessions"] == 2
