"""Experimentally verified EPSON L1800 values.

Source: "LocalPrintServer — EPSON L1800 Final Developer Master Specification v1.0".
Every number in this module was captured from the installed driver on the target
server. Do NOT add or change values without new verification on that machine.
Only standard public DEVMODE fields are used; Epson private DriverData is never
modified (spec §15–§21, §36).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PaperSize:
    key: str
    dm_paper: int          # DEVMODE.PaperSize
    width_01mm: int        # DEVMODE.PaperWidth (0.1 mm), portrait
    length_01mm: int       # DEVMODE.PaperLength (0.1 mm), portrait
    label: str

    @property
    def width_mm(self) -> float:
        return self.width_01mm / 10.0

    @property
    def length_mm(self) -> float:
        return self.length_01mm / 10.0


# Spec §6 (order = UI order from spec §30). B3 / A2 intentionally absent: they were
# observed only as document sizes that trigger scaling, not physical output paper.
PAPER_SIZES: list[PaperSize] = [
    PaperSize("A4", 9, 2100, 2970, "A4 (210 × 297 mm)"),
    PaperSize("A5", 11, 1480, 2100, "A5 (148 × 210 mm)"),
    PaperSize("A6", 70, 1050, 1480, "A6 (105 × 148 mm)"),
    PaperSize("A3", 8, 2970, 4200, "A3 (297 × 420 mm)"),
    PaperSize("A3PLUS", 258, 3290, 4830, "A3+ (329 × 483 mm)"),
    PaperSize("B4", 12, 2570, 3640, "B4 (257 × 364 mm)"),
    PaperSize("B5", 13, 1820, 2570, "B5 (182 × 257 mm)"),
    PaperSize("P9X13", 281, 890, 1270, "9 × 13 cm"),
    PaperSize("P10X15", 285, 1016, 1524, "10 × 15 cm"),
    PaperSize("P13X18", 284, 1270, 1780, "13 × 18 cm"),
    PaperSize("P13X20", 267, 1270, 2032, "13 × 20 cm"),
    PaperSize("P20X25", 268, 2032, 2540, "20 × 25 cm"),
    PaperSize("WIDE16X9", 306, 1016, 1806, "16:9 Wide (102 × 181 mm)"),
    PaperSize("P100X148", 263, 1000, 1480, "100 × 148 mm"),
    PaperSize("LETTER", 1, 2159, 2794, "Letter (8.5 × 11 in)"),
]
PAPER_BY_KEY = {p.key: p for p in PAPER_SIZES}

# Spec §7
CUSTOM_PAPER_KEY = "CUSTOM"
CUSTOM_DM_PAPER = 256
CUSTOM_MIN_W_MM, CUSTOM_MAX_W_MM = 89.0, 329.0
CUSTOM_MIN_H_MM, CUSTOM_MAX_H_MM = 127.0, 1117.6


@dataclass(frozen=True)
class MediaType:
    key: str
    dm_media: int          # DEVMODE.MediaType
    native_dpi: int        # resolution observed with this media (informational)
    label: str


# Spec §8 — captured from the actual installed driver.
MEDIA_TYPES: list[MediaType] = [
    MediaType("PLAIN", 1, 360, "Plain Paper"),
    MediaType("ULTRA_GLOSSY", 352, 720, "Epson Ultra Glossy"),
    MediaType("PREMIUM_GLOSSY", 325, 720, "Epson Premium Glossy"),
    MediaType("PREMIUM_SEMIGLOSS", 275, 720, "Epson Premium Semigloss"),
    MediaType("PHOTO_GLOSSY", 396, 720, "Photo Paper Glossy"),
    MediaType("MATTE", 326, 720, "Epson Matte"),
    MediaType("PHOTO_QUALITY_INKJET", 318, 720, "Epson Photo Quality Ink Jet"),
    MediaType("PHOTO_STICKERS", 338, 720, "Epson Photo Stickers"),
    MediaType("ENVELOPES", 353, 360, "Envelopes"),
]
MEDIA_BY_KEY = {m.key: m for m in MEDIA_TYPES}

# Spec §9: (PrintQuality, YResolution). No "Advanced" quality exists in the Epson UI.
QUALITY = {
    "DRAFT": (180, 180),
    "STANDARD": (360, 360),
    "HIGH": (720, 720),
}
QUALITY_ORDER = ["DRAFT", "STANDARD", "HIGH"]

# Spec §5, §10, §11
COLOR = {"COLOR": 2, "MONO": 1}
ORIENTATION = {"PORTRAIT": 1, "LANDSCAPE": 2}
ORIENTATION_CHOICES = ["AUTO", "PORTRAIT", "LANDSCAPE"]
DUPLEX_SIMPLEX = 1           # Only one-sided is supported; duplex is never exposed.

# Spec §12
SCALING_CHOICES = ["FIT", "FILL", "ACTUAL"]
# Spec §18: N-up is composed by the application (DEVMODE.Nup stays 0 on this driver),
# so any grid is possible without touching the driver: 2 = 1x2, 4 = 2x2, 6 = 2x3,
# 9 = 3x3, 16 = 4x4 (more columns/rows along the longer side of the sheet).
NUP_CHOICES = [1, 2, 4, 6, 9, 16]

# Spec §19: driver exposes 999 copies; the business limit is an admin setting.
DRIVER_MAX_COPIES = 999

# Standard DEVMODE Fields flags (wingdi.h)
DM_ORIENTATION = 0x00000001
DM_PAPERSIZE = 0x00000002
DM_PAPERLENGTH = 0x00000004
DM_PAPERWIDTH = 0x00000008
DM_COPIES = 0x00000100
DM_PRINTQUALITY = 0x00000400
DM_COLOR = 0x00000800
DM_DUPLEX = 0x00001000
DM_YRESOLUTION = 0x00002000
DM_COLLATE = 0x00008000
DM_MEDIATYPE = 0x02000000


def paper_dims_mm(paper_key: str, custom_w_mm: float | None = None,
                  custom_h_mm: float | None = None) -> tuple[float, float]:
    """Portrait (width, height) in mm."""
    if paper_key == CUSTOM_PAPER_KEY:
        return float(custom_w_mm), float(custom_h_mm)
    p = PAPER_BY_KEY[paper_key]
    return p.width_mm, p.length_mm
