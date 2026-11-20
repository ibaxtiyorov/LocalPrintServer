"""Page-range parsing, selection rules and multi-document page sequences."""
import pytest

from lps import catalog
from lps.options import (MAX_SELECTED_PAGES, OptionsError, format_page_range, parse_page_range,
                         validate_options)
from lps.settings_service import SPEC

SETTINGS = {k: v[0] for k, v in SPEC.items()}
ALL = {p.key for p in catalog.PAPER_SIZES}


@pytest.mark.parametrize("text,count,expected", [
    ("", 10, list(range(10))),                                   # all pages
    ("1-5", 10, [0, 1, 2, 3, 4]),                                 # range
    ("1,3,7", 10, [0, 2, 6]),                                     # individual pages
    ("2,5-8,15,20-22", 25, [1, 4, 5, 6, 7, 14, 19, 20, 21]),      # mixed (spec example)
    ("8-", 10, [7, 8, 9]),                                        # open range
    (" 3 , 1 ", 5, [0, 2]),                                       # spaces, any typing order
    ("2,2,1-3", 5, [0, 1, 2]),                                    # duplicates removed
    ("5-5", 5, [4]),
])
def test_parse_valid(text, count, expected):
    assert parse_page_range(text, count) == expected


@pytest.mark.parametrize("text,count", [
    ("0", 5), ("6", 5), ("4-9", 5), ("3-2", 5), ("a", 5), ("1;2", 5), ("-3", 5), ("1--2", 5),
    (",", 5), ("1-2-3", 5), ("x" * 201, 5)])
def test_parse_invalid_against_real_page_count(text, count):
    with pytest.raises(ValueError):
        parse_page_range(text, count)


def test_parse_limit():
    with pytest.raises(ValueError):
        parse_page_range(f"1-{MAX_SELECTED_PAGES + 1}", MAX_SELECTED_PAGES + 1)


@pytest.mark.parametrize("pages,text", [
    ([0, 1, 2, 4, 7, 8], "1-3,5,8-9"), ([4], "5"), ([2, 0, 1], "1-3"), ([], "")])
def test_format_roundtrip(pages, text):
    assert format_page_range(pages) == text
    if pages:
        assert parse_page_range(text, 20) == sorted(set(pages))


def test_multi_document_global_sequence():
    o = validate_options({}, SETTINGS, {}, ALL, [5, 3, 4], ["2,4", "", "1-2"])
    # doc0 pages 2,4 -> global 1,3 ; doc1 all -> 5,6,7 ; doc2 pages 1-2 -> 8,9
    assert o.pages == [1, 3, 5, 6, 7, 8, 9]
    assert [d["pages"] for d in o.documents] == [[1, 3], [0, 1, 2], [0, 1]]
    assert o.page_range == ""


def test_multi_document_errors_point_to_the_document():
    with pytest.raises(OptionsError) as e:
        validate_options({}, SETTINGS, {}, ALL, [5, 3], ["1-2", "4"])
    assert e.value.errors == {"page_range.1": "err.page_range"}


def test_single_document_keeps_v1_field_name():
    with pytest.raises(OptionsError) as e:
        validate_options({"page_range": "9"}, SETTINGS, {}, ALL, 3)
    assert e.value.errors == {"page_range": "err.page_range"}


@pytest.mark.parametrize("nup", [1, 2, 4, 6, 9, 16])
def test_all_nup_values_accepted(nup):
    assert validate_options({"nup": nup}, SETTINGS, {}, ALL, 1).nup == nup


@pytest.mark.parametrize("nup", [3, 5, 8, 12, 32, 0])
def test_other_nup_values_rejected(nup):
    with pytest.raises(OptionsError):
        validate_options({"nup": nup}, SETTINGS, {}, ALL, 1)
