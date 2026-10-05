"""The raw-line filter: decide before building the record, not after.

A ``goa_uniprot_all`` release carries 280 million annotations and a caller
loading into a bounded protein universe keeps almost none of them: measured on
GOA 156, 671.138 records out of 280.922.738 lines, 0,24%. Building a validated
record for every line and dropping the other 99,76% is the dominant cost of the
scan, so the predicate has to run on the split columns. Measured through the
plugin, that is 11,5 minutes per release against 4,4.

These tests pin the two properties that make it worth having: the record is NOT
constructed for a rejected line, and the predicate sees raw strings.
"""

from unittest.mock import patch

from protea_sources.goa import parse_gaf_line, parse_gaf_text

# accession at idx 1, qualifier 3, go_id 4, evidence 6
COLS = ["UniProtKB", "P12345", "SYM", "", "GO:0005515", "PMID:1", "IDA", "", "F",
        "name", "syn", "protein", "taxon:9606", "20200101", "UniProt"]


def line(**over):
    cols = list(COLS)
    for idx, val in over.items():
        cols[int(idx)] = val
    return "\t".join(cols)


class TestItDecidesBeforeBuilding:
    def test_a_rejected_line_never_constructs_a_record(self):
        """The whole point. If the record were built and then dropped, the
        filter would cost MORE than no filter at all."""
        with patch("protea_sources.goa.GoaAnnotationRecord") as ctor:
            assert parse_gaf_line(line(), accept=lambda c: False) is None
            ctor.assert_not_called()

    def test_an_accepted_line_still_constructs_one(self):
        rec = parse_gaf_line(line(), accept=lambda c: True)
        assert rec is not None
        assert rec.accession == "P12345"
        assert rec.evidence_code == "IDA"

    def test_without_a_predicate_nothing_changes(self):
        """Default None keeps every existing caller behaving as before."""
        rec = parse_gaf_line(line())
        assert rec is not None and rec.accession == "P12345"


class TestWhatThePredicateSees:
    def test_it_receives_the_raw_split_columns(self):
        seen = []
        parse_gaf_line(line(), accept=lambda c: seen.append(c) or True)
        assert len(seen) == 1
        assert seen[0][1] == "P12345", "accession at index 1"
        assert seen[0][6] == "IDA", "evidence code at index 6"

    def test_empty_fields_arrive_as_empty_strings_not_none(self):
        """Documented and pinned: the predicate runs before the ``or None``
        normalisation the record applies, so a caller testing a qualifier must
        compare against "" and not None."""
        seen = []
        parse_gaf_line(line(**{"3": ""}), accept=lambda c: seen.append(c[3]) or True)
        assert seen == [""]

    def test_it_is_not_called_for_comments_or_short_lines(self):
        """Those are rejected earlier and must not cost a predicate call."""
        calls = []

        def spy(cols):
            calls.append(cols)
            return True

        assert parse_gaf_line("! a comment", accept=spy) is None
        assert parse_gaf_line("too\tfew\tcolumns", accept=spy) is None
        assert calls == []


class TestItFlowsThroughParseGafText:
    def test_the_predicate_filters_a_whole_text(self):
        text = "\n".join([line(**{"1": "P00001"}), line(**{"1": "P00002"}),
                          line(**{"1": "P00003"})])
        keep = {"P00001", "P00003"}
        got = [r.accession for r in parse_gaf_text(text, accept=lambda c: c[1] in keep)]
        assert got == ["P00001", "P00003"]
