"""Controlled application test page (Admin > Printer > Maintenance).

A4 at 300 dpi, printed with scaling=ACTUAL so the 100 mm square can be measured
with a ruler (matches the verified actual-size check in spec §12).
"""
from __future__ import annotations

import socket
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

DPI = 300


def _mm(v: float) -> int:
    return round(v / 25.4 * DPI)


def _font(size_px: int):
    for name in ("arial.ttf", "segoeui.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size_px)
        except OSError:
            continue
    return ImageFont.load_default()


def make_test_page(path: Path, printer_name: str, color: bool, actor: str) -> Path:
    w, h = _mm(210), _mm(297)
    im = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(im)
    big, small = _font(_mm(7)), _font(_mm(3.5))
    d.text((_mm(15), _mm(15)), "LocalPrintServer — test page", fill="black", font=big)
    lines = [f"Printer: {printer_name}", f"Host: {socket.gethostname()}",
             f"Time: {datetime.now():%Y-%m-%d %H:%M:%S}", f"Requested by: {actor}",
             f"Mode: {'colour' if color else 'black & white'}, A4, actual size (100%)"]
    for i, ln in enumerate(lines):
        d.text((_mm(15), _mm(28) + i * _mm(6)), ln, fill="black", font=small)
    # 100 x 100 mm square with 10 mm ticks
    x0, y0 = _mm(55), _mm(70)
    d.rectangle((x0, y0, x0 + _mm(100), y0 + _mm(100)), outline="black", width=_mm(0.4))
    for k in range(11):
        t = _mm(k * 10)
        d.line((x0 + t, y0, x0 + t, y0 + _mm(4 if k % 5 else 7)), fill="black", width=3)
        d.line((x0, y0 + t, x0 + _mm(4 if k % 5 else 7), y0 + t), fill="black", width=3)
    d.text((x0, y0 + _mm(102)), "This square must measure 100 × 100 mm", fill="black", font=small)
    # Colour / grey bars
    bars = ([(0, 255, 255), (255, 0, 255), (255, 255, 0), (0, 0, 0), (255, 0, 0), (0, 160, 0),
             (0, 0, 255)] if color else [(v, v, v) for v in (0, 40, 80, 120, 160, 200, 235)])
    bw = _mm(180 / len(bars))
    for i, c in enumerate(bars):
        d.rectangle((_mm(15) + i * bw, _mm(190), _mm(15) + (i + 1) * bw - 4, _mm(215)), fill=c)
    for i in range(256):
        x = _mm(15) + round(i * _mm(180) / 256)
        d.rectangle((x, _mm(222), x + _mm(180) // 256 + 1, _mm(232)), fill=(i, i, i))
    # Edge markers 5 mm from each edge (show where the non-printable area is).
    for (x, y) in ((5, 5), (205, 5), (5, 292), (205, 292)):
        cx, cy = _mm(x), _mm(y)
        d.line((cx - _mm(3), cy, cx + _mm(3), cy), fill="black", width=3)
        d.line((cx, cy - _mm(3), cx, cy + _mm(3)), fill="black", width=3)
    d.text((_mm(15), _mm(245)), "Crosses are 5 mm from each paper edge.", fill="black", font=small)
    im.save(path, "PNG", dpi=(DPI, DPI))
    return path
