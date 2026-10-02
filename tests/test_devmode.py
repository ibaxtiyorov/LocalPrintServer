"""DEVMODE construction: verified values only, standard public fields only."""
import pytest

from lps import catalog
from lps.options import PrintOptions
from lps.printer.base import build_device_settings


def ds(**kw):
    o = PrintOptions(**{k: v for k, v in kw.items() if k not in ("orientation_resolved", "driver")},
                     pages=[0])
    return build_device_settings(o, kw.get("orientation_resolved", "PORTRAIT"),
                                 driver_copies=kw.get("driver", True))


@pytest.mark.parametrize("key,dm,w,l", [
    ("A4", 9, 2100, 2970), ("A5", 11, 1480, 2100), ("A6", 70, 1050, 1480), ("A3", 8, 2970, 4200),
    ("B4", 12, 2570, 3640), ("B5", 13, 1820, 2570), ("A3PLUS", 258, 3290, 4830),
    ("P9X13", 281, 890, 1270), ("P10X15", 285, 1016, 1524), ("P13X18", 284, 1270, 1780),
    ("P13X20", 267, 1270, 2032), ("P20X25", 268, 2032, 2540), ("WIDE16X9", 306, 1016, 1806),
    ("P100X148", 263, 1000, 1480), ("LETTER", 1, 2159, 2794)])
def test_paper_mapping_matches_spec_section_6(key, dm, w, l):
    s = ds(paper=key)
    assert (s.paper, s.width_01mm, s.length_01mm, s.custom) == (dm, w, l, False)


def test_custom_paper_256():
    s = ds(paper="CUSTOM", custom_w_mm=200, custom_h_mm=290)
    assert (s.paper, s.width_01mm, s.length_01mm, s.custom) == (256, 2000, 2900, True)


@pytest.mark.parametrize("key,dm", [("PLAIN", 1), ("ULTRA_GLOSSY", 352), ("PREMIUM_GLOSSY", 325),
                                    ("PREMIUM_SEMIGLOSS", 275), ("PHOTO_GLOSSY", 396), ("MATTE", 326),
                                    ("PHOTO_QUALITY_INKJET", 318), ("PHOTO_STICKERS", 338),
                                    ("ENVELOPES", 353)])
def test_media_mapping_spec_section_8(key, dm):
    assert ds(media=key).media == dm


@pytest.mark.parametrize("q,pq", [("DRAFT", 180), ("STANDARD", 360), ("HIGH", 720)])
def test_quality_spec_section_9(q, pq):
    s = ds(quality=q)
    assert (s.print_quality, s.y_resolution) == (pq, pq)


def test_color_and_orientation():
    assert ds(color="COLOR").color == 2 and ds(color="MONO").color == 1
    assert ds(orientation_resolved="PORTRAIT").orientation == 1
    assert ds(orientation_resolved="LANDSCAPE").orientation == 2


def test_copies_driver_vs_application():
    s = ds(copies=3, collate=True)
    assert (s.copies, s.collate) == (3, True)
    s = ds(copies=3, collate=True, driver=False)
    assert (s.copies, s.collate) == (1, False)


class FakeDevMode:
    def __init__(self):
        self.Fields = 0x1
        self.PaperSize, self.PaperWidth, self.PaperLength = 1, 2159, 2794
        self.Orientation, self.MediaType, self.PrintQuality, self.YResolution = 1, 1, -3, 0
        self.Color, self.Copies, self.Collate, self.Duplex = 2, 1, 1, 1
        self.DriverExtra, self.DeviceName = 4880, "EPSON L1800 Series"
        self.DriverData = b"\x00" * 16          # must never be modified


def test_win32_devmode_writes_only_public_fields(monkeypatch):
    pytest.importorskip("win32print")
    from lps.printer import win32_backend as wb

    dm = FakeDevMode()
    calls = []
    monkeypatch.setattr(wb.win32print, "OpenPrinter", lambda *a, **k: "h")
    monkeypatch.setattr(wb.win32print, "ClosePrinter", lambda h: None)
    monkeypatch.setattr(wb.win32print, "GetPrinter", lambda h, lvl: {"pDevMode": dm})
    monkeypatch.setattr(wb.win32print, "DocumentProperties",
                        lambda *a: calls.append(("DocumentProperties", a[5])) or 1)
    monkeypatch.setattr(wb.win32print, "SetPrinter",
                        lambda *a: pytest.fail("SetPrinter must never be called"))
    b = wb.Win32Backend("EPSON L1800 Series", usb_probe_enabled=False)
    o = PrintOptions(paper="CUSTOM", custom_w_mm=200, custom_h_mm=250, media="PREMIUM_GLOSSY",
                     quality="HIGH", color="MONO", copies=2, collate=True, pages=[0])
    s = build_device_settings(o, "LANDSCAPE", driver_copies=True)
    out, report = b._build_devmode(s, merge=True)
    assert out is dm
    assert (dm.PaperSize, dm.PaperWidth, dm.PaperLength) == (256, 2000, 2500)
    assert (dm.Orientation, dm.MediaType, dm.PrintQuality, dm.YResolution) == (2, 325, 720, 720)
    assert (dm.Color, dm.Copies, dm.Collate, dm.Duplex) == (1, 2, 1, 1)
    need = (catalog.DM_ORIENTATION | catalog.DM_PAPERSIZE | catalog.DM_PAPERWIDTH
            | catalog.DM_PAPERLENGTH | catalog.DM_MEDIATYPE | catalog.DM_PRINTQUALITY
            | catalog.DM_YRESOLUTION | catalog.DM_COLOR | catalog.DM_COPIES | catalog.DM_COLLATE)
    assert dm.Fields & need == need
    assert dm.DriverData == b"\x00" * 16
    assert calls == [("DocumentProperties", 8 | 2)]      # DM_IN_BUFFER | DM_OUT_BUFFER

    # standard size clears the custom width/length flags
    dm2 = FakeDevMode()
    dm2.Fields = 0xFFFFFFFF
    monkeypatch.setattr(wb.win32print, "GetPrinter", lambda h, lvl: {"pDevMode": dm2})
    b._build_devmode(build_device_settings(PrintOptions(paper="A4", pages=[0]), "PORTRAIT",
                                           driver_copies=True), merge=False)
    assert dm2.PaperSize == 9
    assert not dm2.Fields & (catalog.DM_PAPERWIDTH | catalog.DM_PAPERLENGTH)


def test_borderless_detection_from_driver_names(monkeypatch):
    pytest.importorskip("win32print")
    from lps.printer import win32_backend as wb
    names = {9: "A4 210 x 297 mm", 285: "10 x 15 cm (4 x 6 in)", 900: "A4 210 x 297 mm (Borderless)",
             901: "10 x 15 cm (4 x 6 in) (без полей)", 902: "Unrelated (Borderless)"}
    ids = list(names)
    monkeypatch.setattr(wb.win32print, "OpenPrinter", lambda *a, **k: "h")
    monkeypatch.setattr(wb.win32print, "ClosePrinter", lambda h: None)
    monkeypatch.setattr(wb.win32print, "GetPrinter", lambda h, lvl: {"pPortName": "USB001"})

    def devcaps(name, port, cap):
        return {wb.DC_PAPERS: ids, wb.DC_PAPERNAMES: [names[i] for i in ids],
                wb.DC_PAPERSIZE: [{"x": 2100, "y": 2970}] * len(ids)}.get(cap, [])
    monkeypatch.setattr(wb.win32print, "DeviceCapabilities", devcaps)
    b = wb.Win32Backend("EPSON L1800 Series", usb_probe_enabled=False)
    caps = b.capabilities(refresh=True)
    assert caps["borderless"] == {"A4": 900, "P10X15": 901}
