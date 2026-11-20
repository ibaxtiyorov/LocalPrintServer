"""Zero-paper driver verification (Admin > Printer > Driver check).

For every verified paper size and every media x quality combination, builds the
job DEVMODE exactly as a real job would, lets the driver validate it and opens a
DC to read the geometry — but never calls StartDoc, so no spooler job is created
and no paper is used. Reports any public field the driver changed and any DC
size that differs from the nominal paper size.
"""
from __future__ import annotations

import logging

from . import catalog
from .options import PrintOptions
from .printer.base import PrinterBackend, PrinterError, build_device_settings

log = logging.getLogger("lps.diagnostics")


def _mm(px: int, dpi: int) -> float:
    return round(px / dpi * 25.4, 1) if dpi else 0.0


def driver_check(backend: PrinterBackend, merge: bool) -> dict:
    papers, media = [], []
    candidates = [(p.key, p.label, None, None) for p in catalog.PAPER_SIZES]
    candidates.append(("CUSTOM", "Custom 200 x 250 mm (spec §13)", 200.0, 250.0))
    for key, label, cw, ch in candidates:
        o = PrintOptions(paper=key, custom_w_mm=cw, custom_h_mm=ch, pages=[0])
        ds = build_device_settings(o, "PORTRAIT", driver_copies=True)
        w, h = o.paper_dims_mm()
        row = {"paper": key, "label": label, "nominal_mm": [w, h]}
        try:
            g, rep = backend.probe(ds, merge)
            row.update(dc_mm=[_mm(g.phys_w, g.dpi_x), _mm(g.phys_h, g.dpi_y)],
                       printable_mm=[_mm(g.horz, g.dpi_x), _mm(g.vert, g.dpi_y)],
                       offset_px=[g.off_x, g.off_y], dpi=[g.dpi_x, g.dpi_y],
                       driver_adjusted=rep.get("driver_adjusted") or {})
            dw, dh = row["dc_mm"]
            row["size_ok"] = abs(dw - w) <= 3 and abs(dh - h) <= 3
        except PrinterError as e:
            row.update(error=e.code, detail=e.detail[:200])
        papers.append(row)
    for m in catalog.MEDIA_TYPES:
        for q in catalog.QUALITY_ORDER:
            o = PrintOptions(paper="A4", media=m.key, quality=q, pages=[0])
            ds = build_device_settings(o, "PORTRAIT", driver_copies=True)
            row = {"media": m.key, "label": m.label, "quality": q,
                   "requested": [m.dm_media, *catalog.QUALITY[q]]}
            try:
                g, rep = backend.probe(ds, merge)
                row.update(dpi=[g.dpi_x, g.dpi_y], driver_adjusted=rep.get("driver_adjusted") or {},
                           dc_devmode=rep.get("dc_devmode"))
            except PrinterError as e:
                row.update(error=e.code, detail=e.detail[:200])
            media.append(row)
    adjusted = sum(1 for r in papers + media if r.get("driver_adjusted"))
    errors = sum(1 for r in papers + media if r.get("error"))
    mismatched = sum(1 for r in papers if r.get("size_ok") is False)
    log.info("Driver check (merge=%s): %d paper, %d media/quality probes; %d adjusted by driver,"
             " %d size mismatches, %d errors", merge, len(papers), len(media), adjusted,
             mismatched, errors)
    for r in papers + media:
        if r.get("driver_adjusted") or r.get("error") or r.get("size_ok") is False:
            log.warning("Driver check detail: %s", r)
    return {"merge": merge, "papers": papers, "media": media,
            "summary": {"adjusted": adjusted, "errors": errors, "size_mismatches": mismatched}}
