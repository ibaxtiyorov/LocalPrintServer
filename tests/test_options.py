import pytest

from lps import catalog
from lps.options import OptionsError, parse_page_range, validate_options
from lps.settings_service import SPEC

SETTINGS = {k: v[0] for k, v in SPEC.items()}
ALL = {p.key for p in catalog.PAPER_SIZES}


def v(raw, pages=3, borderless=None, settings=None, available=ALL):
    return validate_options(raw, settings or SETTINGS, borderless or {}, available, pages)


def errors(raw, **kw):
    with pytest.raises(OptionsError) as ei:
        v(raw, **kw)
    return ei.value.errors


def test_defaults():
    o = v({})
    assert (o.paper, o.media, o.quality, o.color, o.scaling) == ("A4", "PLAIN", "STANDARD", "COLOR", "FIT")
    assert o.pages == [0, 1, 2]


@pytest.mark.parametrize("w,h,ok", [(89, 127, True), (329, 1117.6, True), (200, 290, True),
                                    (88.9, 200, False), (330, 200, False), (100, 126.9, False),
                                    (100, 1117.7, False)])
def test_custom_range_spec_section_7(w, h, ok):
    raw = {"paper": "CUSTOM", "custom_w_mm": w, "custom_h_mm": h}
    if ok:
        assert v(raw).custom_w_mm == w
    else:
        assert errors(raw) == {"custom": "err.custom_size"}


def test_b3_a2_not_accepted():
    assert "paper" in errors({"paper": "B3"})
    assert "paper" in errors({"paper": "A2"})


def test_unknown_values_rejected():
    e = errors({"media": "GLOSSY_FAKE", "quality": "ADVANCED", "color": "SEPIA", "orientation": "X",
                "scaling": "STRETCH", "nup": 3})
    assert set(e) == {"media", "quality", "color", "orientation", "scaling", "nup"}


def test_copies_limits():
    assert v({"copies": 50}).copies == 50
    assert errors({"copies": 51}) == {"copies": "err.copies"}
    assert errors({"copies": 0}) == {"copies": "err.copies"}
    assert errors({"copies": "abc"}) == {"copies": "err.copies"}


def test_nup_with_actual_rejected():
    assert errors({"nup": 2, "scaling": "ACTUAL"}) == {"scaling": "err.nup_actual"}


def test_borderless_requires_setting_and_driver_paper():
    assert "borderless" in errors({"borderless": True})
    s = dict(SETTINGS, borderless_enabled=True)
    assert "borderless" in errors({"borderless": True, "paper": "A3"}, settings=s, borderless={"A4": 1})
    o = v({"borderless": True, "paper": "A4", "margins_mm": 10}, settings=s, borderless={"A4": 1})
    assert o.borderless and o.margins_mm == 0


def test_paper_not_advertised_by_driver():
    assert errors({"paper": "A3PLUS"}, available=ALL - {"A3PLUS"}) == {"paper": "err.paper_unavailable"}


def test_raw_devmode_fields_ignored():
    o = v({"DriverData": "AAAA", "PaperSize": 999, "Fields": 1})
    assert not hasattr(o, "DriverData")


@pytest.mark.parametrize("text,count,expected", [
    ("", 3, [0, 1, 2]), ("2", 3, [1]), ("1-2", 5, [0, 1]), ("2-", 4, [1, 2, 3]),
    ("1, 3-4", 5, [0, 2, 3])])
def test_page_range(text, count, expected):
    assert parse_page_range(text, count) == expected


@pytest.mark.parametrize("text", ["0", "4", "3-2", "a", "1;2", "-3"])
def test_page_range_invalid(text):
    with pytest.raises(ValueError):
        parse_page_range(text, 3)


def test_margins_must_leave_room_on_small_paper():
    small = {"paper": "CUSTOM", "custom_w_mm": 89, "custom_h_mm": 127}
    assert errors({**small, "margins_mm": 45}) == {"margins_mm": "err.margins"}
    assert v({**small, "margins_mm": 30}).margins_mm == 30
    assert v({"paper": "A4", "margins_mm": 50}).margins_mm == 50        # plenty of room on A4
    s = dict(SETTINGS, borderless_enabled=True)                          # ignored when borderless
    assert v({"paper": "A4", "borderless": True, "margins_mm": 50}, settings=s,
             borderless={"A4": 1}).margins_mm == 0
