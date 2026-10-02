"""Turns a document + options + geometry into sheet images.

Used by both the print worker (real DC geometry) and the browser preview
(driver geometry when available, otherwise estimated), so both follow exactly
the same layout rules.
"""
from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageOps

from .documents import Document
from .layout import (Geometry, JobPlan, PageInfo, Placement, SheetPlan, layout_sheet,
                     plan_job, resolve_orientation)
from .options import PrintOptions

# Upper bound per placed image sent to GDI; larger destinations are stretched by GDI.
MAX_PLACEMENT_PIXELS = 50_000_000


@dataclass
class PreparedJob:
    options: PrintOptions
    orientation: str
    plan: JobPlan
    infos: dict[int, PageInfo]


def oriented_sheet_mm(opts: PrintOptions, orientation: str) -> tuple[float, float]:
    """Sheet size as seen by the DC. Portrait = DEVMODE PaperWidth x PaperLength (as fed);
    a custom width may exceed its length, so the dimensions are not normalised."""
    w, h = opts.paper_dims_mm()
    return (h, w) if orientation == "LANDSCAPE" else (w, h)


def prepare(doc: Document, opts: PrintOptions) -> PreparedJob:
    infos = {i: doc.page_info(i) for i in dict.fromkeys(opts.pages)}
    first = infos[opts.pages[0]] if opts.pages else None
    pw, ph = opts.paper_dims_mm()
    orientation = resolve_orientation(opts.orientation, first, pw, ph, opts.nup)
    plan = plan_job(opts.pages, opts.nup, opts.reverse, orientation)
    return PreparedJob(opts, orientation, plan, infos)


def plan_sheet(prep: PreparedJob, g: Geometry, sheet_index: int, gap_mm: float) -> SheetPlan:
    o = prep.options
    pages = [(i, prep.infos[i]) for i in prep.plan.sheets[sheet_index]]
    return layout_sheet(g, pages, scaling=o.scaling, margins_mm=o.margins_mm, nup=o.nup,
                        gap_mm=gap_mm, auto_rotate=(o.orientation == "AUTO"),
                        mirror=o.mirror, rotate180=o.rotate180)


def placement_image(doc: Document, p: Placement, dest_w: float, dest_h: float,
                    grayscale: bool) -> Image.Image:
    """Source page -> rotated -> cropped -> downscaled -> flipped image for `p.dest`."""
    budget = 1.0
    if dest_w * dest_h > MAX_PLACEMENT_PIXELS:
        budget = (MAX_PLACEMENT_PIXELS / (dest_w * dest_h)) ** 0.5
    tw, th = max(1, dest_w * budget), max(1, dest_h * budget)
    cw = max(1e-6, p.crop[2] - p.crop[0])
    ch = max(1e-6, p.crop[3] - p.crop[1])
    # Size of the full (uncropped, rotated) page needed to supply tw x th after cropping.
    full_w, full_h = tw / cw, th / ch
    if p.rotate == 90:
        src = doc.render_page(p.page_index, round(full_h), round(full_w))
        src = src.transpose(Image.Transpose.ROTATE_90)
    else:
        src = doc.render_page(p.page_index, round(full_w), round(full_h))
    box = (round(p.crop[0] * src.width), round(p.crop[1] * src.height),
           round(p.crop[2] * src.width), round(p.crop[3] * src.height))
    if box != (0, 0, src.width, src.height):
        box = (box[0], box[1], max(box[2], box[0] + 1), max(box[3], box[1] + 1))
        src = src.crop(box)
    if src.width > tw * 1.01 or src.height > th * 1.01:
        src = src.resize((max(1, round(tw)), max(1, round(th))), Image.LANCZOS)
    if p.flip_h:
        src = src.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    if p.flip_v:
        src = src.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    if grayscale:
        src = ImageOps.grayscale(src).convert("RGB")
    return src


def render_preview(doc: Document, prep: PreparedJob, g: Geometry, sheet_index: int,
                   gap_mm: float, max_px: int = 900) -> tuple[bytes, dict]:
    """PNG layout preview of one sheet + metadata (not a raster-exact Epson preview)."""
    sp = plan_sheet(prep, g, sheet_index, gap_mm)
    s = max_px / max(g.phys_w, g.phys_h)
    W, H = max(1, round(g.phys_w * s)), max(1, round(g.phys_h * s))
    canvas = Image.new("RGB", (W, H), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)

    def sc(r):
        return (r[0] * s, r[1] * s, r[2] * s, r[3] * s)

    # Non-printable border (grey), printable area, content area and cells.
    px = sc(sp.printable)
    if px != (0, 0, W, H):
        draw.rectangle((0, 0, W, H), fill=(226, 229, 234))
        draw.rectangle(px, fill=(255, 255, 255))
    grayscale = prep.options.color == "MONO"
    for p in sp.placements:
        d = sc(p.dest)
        dw, dh = max(1, round(d[2] - d[0])), max(1, round(d[3] - d[1]))
        im = placement_image(doc, p, dw, dh, grayscale)
        if im.size != (dw, dh):
            im = im.resize((dw, dh), Image.BILINEAR)
        canvas.paste(im, (round(d[0]), round(d[1])))
    if prep.options.nup > 1 or prep.options.margins_mm > 0:
        for c in sp.cells:
            _dashed_rect(draw, sc(c), (120, 140, 170))
    _dashed_rect(draw, px, (230, 90, 90))
    buf = io.BytesIO()
    canvas.save(buf, "PNG", optimize=False)
    meta = {
        "sheet": sheet_index,
        "sheets": len(prep.plan.sheets),
        "orientation": prep.orientation,
        "geometry": g.to_dict(),
        "clipped": any(p.clipped for p in sp.placements),
        "width": W, "height": H,
    }
    return buf.getvalue(), meta


def _dashed_rect(draw: ImageDraw.ImageDraw, r, color, dash=6):
    x0, y0, x1, y1 = r
    x1 -= 1
    y1 -= 1
    for x in range(int(x0), int(x1), dash * 2):
        draw.line((x, y0, min(x + dash, x1), y0), fill=color)
        draw.line((x, y1, min(x + dash, x1), y1), fill=color)
    for y in range(int(y0), int(y1), dash * 2):
        draw.line((x0, y, x0, min(y + dash, y1)), fill=color)
        draw.line((x1, y, x1, min(y + dash, y1)), fill=color)
