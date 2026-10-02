"""Deterministic page layout (pure functions, no I/O).

All coordinates are in *physical device pixels*: (0,0) is the physical top-left
corner of the sheet; the printable area starts at (PHYSICALOFFSETX, PHYSICALOFFSETY).
The same plan drives both the browser preview and the real printer DC, so the
preview matches what is sent to the driver (spec §31).

Mirror / 180° rotation / N-up / page order are done here, on the application
side, because the Epson driver keeps those in shared private DriverData bytes
that must not be patched (spec §17, §18, §21).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

Rect = tuple[float, float, float, float]  # x0, y0, x1, y1

MM_PER_INCH = 25.4
# Estimated geometry is used only for previews when the driver cannot be queried.
# 3 mm non-printable margin matches the spec §13 custom 200x250 mm example
# (offset 44 px, HORZRES = PHYSICALWIDTH - 88).
EST_MARGIN_MM = 3.0
# Borderless DC example (spec §14): A4 -> 6026 x 8523 px @720 dpi = 212.6 x 300.7 mm,
# i.e. about 1.3 mm (horizontal) and 1.8 mm (vertical) expansion per side.
EST_BORDERLESS_EXPANSION_X_MM = 1.3
EST_BORDERLESS_EXPANSION_Y_MM = 1.83


class LayoutError(ValueError):
    """The requested layout cannot be placed on the sheet (e.g. margins too large)."""


@dataclass(frozen=True)
class Geometry:
    phys_w: int
    phys_h: int
    off_x: int
    off_y: int
    horz: int
    vert: int
    dpi_x: int
    dpi_y: int
    source: str = "driver"   # "driver" = GetDeviceCaps, "estimated" = computed

    @property
    def printable(self) -> Rect:
        return (self.off_x, self.off_y, self.off_x + self.horz, self.off_y + self.vert)

    @property
    def sheet(self) -> Rect:
        return (0, 0, self.phys_w, self.phys_h)

    def mm_to_px_x(self, mm: float) -> float:
        return mm * self.dpi_x / MM_PER_INCH

    def mm_to_px_y(self, mm: float) -> float:
        return mm * self.dpi_y / MM_PER_INCH

    def to_dict(self) -> dict:
        return dict(phys_w=self.phys_w, phys_h=self.phys_h, off_x=self.off_x, off_y=self.off_y,
                    horz=self.horz, vert=self.vert, dpi_x=self.dpi_x, dpi_y=self.dpi_y,
                    source=self.source)


def estimate_geometry(sheet_w_mm: float, sheet_h_mm: float, dpi: int,
                      borderless: bool = False) -> Geometry:
    """Approximate DC geometry for an already-oriented sheet (preview fallback)."""
    if borderless:
        w_mm = sheet_w_mm + 2 * EST_BORDERLESS_EXPANSION_X_MM
        h_mm = sheet_h_mm + 2 * EST_BORDERLESS_EXPANSION_Y_MM
        pw, ph = round(w_mm * dpi / MM_PER_INCH), round(h_mm * dpi / MM_PER_INCH)
        return Geometry(pw, ph, 0, 0, pw, ph, dpi, dpi, "estimated")
    pw, ph = round(sheet_w_mm * dpi / MM_PER_INCH), round(sheet_h_mm * dpi / MM_PER_INCH)
    m = round(EST_MARGIN_MM * dpi / MM_PER_INCH)
    return Geometry(pw, ph, m, m, pw - 2 * m, ph - 2 * m, dpi, dpi, "estimated")


@dataclass(frozen=True)
class PageInfo:
    """Natural size of a source page. mm sizes are None when unknown."""
    width_mm: float
    height_mm: float

    @property
    def aspect(self) -> float:
        return self.width_mm / self.height_mm

    @property
    def is_landscape(self) -> bool:
        return self.width_mm > self.height_mm


@dataclass
class Placement:
    page_index: int                  # index into the document's pages
    rotate: int                      # CCW degrees applied to the source first: 0 or 90
    crop: Rect                       # normalized (0..1) crop of the rotated source
    dest: Rect                       # physical device px (after sheet transform)
    flip_h: bool = False             # applied after crop (sheet transform)
    flip_v: bool = False             # applied after crop (sheet transform)
    clipped: bool = False            # content cut off by printable area/margins (ACTUAL)


@dataclass
class SheetPlan:
    placements: list[Placement]
    cells: list[Rect]
    content: Rect
    printable: Rect


@dataclass
class JobPlan:
    orientation: str                 # "PORTRAIT" / "LANDSCAPE" (resolved)
    sheets: list[list[int]]          # page indices per sheet in output order (once)
    warnings: list[str] = field(default_factory=list)


def _intersect(a: Rect, b: Rect) -> Rect | None:
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    if x1 <= x0 or y1 <= y0:
        return None
    return (x0, y0, x1, y1)


# N-up grids (columns x rows when the content area is wider than tall). For a taller
# area the grid is transposed, so the larger count always runs along the longer side.
NUP_GRID = {1: (1, 1), 2: (2, 1), 4: (2, 2), 6: (3, 2), 9: (3, 3), 16: (4, 4)}


def grid_for(nup: int, width: float, height: float) -> tuple[int, int]:
    if nup not in NUP_GRID:
        raise ValueError(f"nup must be one of {sorted(NUP_GRID)}")
    cols, rows = NUP_GRID[nup]
    return (cols, rows) if width >= height else (rows, cols)


def split_cells(content: Rect, nup: int, gap_x: float, gap_y: float) -> list[Rect]:
    """Cells in reading order: left-to-right, then top-to-bottom."""
    x0, y0, x1, y1 = content
    w, h = x1 - x0, y1 - y0
    cols, rows = grid_for(nup, w, h)
    if cols * rows == 1:
        return [content]
    gx = gap_x if cols > 1 else 0
    gy = gap_y if rows > 1 else 0
    cw, ch = (w - gx * (cols - 1)) / cols, (h - gy * (rows - 1)) / rows
    if cw <= 0 or ch <= 0:
        raise LayoutError("no room for this many pages per sheet")
    return [(x0 + c * (cw + gx), y0 + r * (ch + gy), x0 + c * (cw + gx) + cw, y0 + r * (ch + gy) + ch)
            for r in range(rows) for c in range(cols)]


def _cell_aspect(sheet_w: float, sheet_h: float, nup: int) -> float:
    cols, rows = grid_for(nup, sheet_w, sheet_h)
    return (sheet_w / cols) / (sheet_h / rows)


def resolve_orientation(requested: str, first_page: PageInfo | None, paper_w_mm: float,
                        paper_h_mm: float, nup: int) -> str:
    """AUTO picks the sheet orientation whose cells best match the first page."""
    if requested in ("PORTRAIT", "LANDSCAPE"):
        return requested
    if first_page is None:
        return "PORTRAIT"
    pw, ph = paper_w_mm, paper_h_mm          # portrait = as fed (PaperWidth x PaperLength)
    target = math.log(first_page.aspect)
    score_p = abs(math.log(_cell_aspect(pw, ph, nup)) - target)
    score_l = abs(math.log(_cell_aspect(ph, pw, nup)) - target)
    return "LANDSCAPE" if score_l < score_p - 1e-9 else "PORTRAIT"


def plan_job(page_indices: list[int], nup: int, reverse: bool, orientation: str) -> JobPlan:
    sheets = [page_indices[i:i + nup] for i in range(0, len(page_indices), nup)]
    if reverse:
        sheets.reverse()
    return JobPlan(orientation=orientation, sheets=sheets)


def output_sequence(num_sheets: int, copies: int, collate: bool) -> list[int]:
    """Sheet order when copies are produced by the application (not the driver)."""
    if collate:
        return [s for _ in range(copies) for s in range(num_sheets)]
    return [s for s in range(num_sheets) for _ in range(copies)]


def _transform_rect(r: Rect, g: Geometry, mirror: bool, rot180: bool) -> Rect:
    x0, y0, x1, y1 = r
    if mirror != rot180:     # exactly one horizontal flip
        x0, x1 = g.phys_w - x1, g.phys_w - x0
    if rot180:
        y0, y1 = g.phys_h - y1, g.phys_h - y0
    return (x0, y0, x1, y1)


def layout_sheet(g: Geometry, pages: list[tuple[int, PageInfo]], *, scaling: str,
                 margins_mm: float, nup: int, gap_mm: float, auto_rotate: bool,
                 mirror: bool = False, rotate180: bool = False) -> SheetPlan:
    """Place up to `nup` pages on one sheet.

    Mirror/180° are handled by laying out in a *virtual* sheet whose printable
    area is the transformed real printable area, then mapping results back. That
    keeps every placement inside the real printable area even when the driver's
    non-printable margins are asymmetric.
    """
    printable = _transform_rect(g.printable, g, mirror, rotate180)
    mx, my = g.mm_to_px_x(margins_mm), g.mm_to_px_y(margins_mm)
    margin_rect = (mx, my, g.phys_w - mx, g.phys_h - my)
    content = _intersect(printable, margin_rect)
    if content is None:
        raise LayoutError("margins leave no printable area")
    cells = split_cells(content, nup, g.mm_to_px_x(gap_mm) if nup > 1 else 0,
                        g.mm_to_px_y(gap_mm) if nup > 1 else 0)

    placements: list[Placement] = []
    for cell, (page_idx, info) in zip(cells, pages):
        cw, ch = cell[2] - cell[0], cell[3] - cell[1]
        pw_mm, ph_mm = info.width_mm, info.height_mm
        rot = 0
        if auto_rotate and abs(pw_mm - ph_mm) > 1e-6 and (pw_mm > ph_mm) != (cw > ch):
            rot = 90
            pw_mm, ph_mm = ph_mm, pw_mm
        pw_px, ph_px = g.mm_to_px_x(pw_mm), g.mm_to_px_y(ph_mm)

        if scaling == "ACTUAL":
            # 1:1 physical size. Single page: centred on the physical sheet so a
            # page matching the paper maps onto the paper exactly (spec §12).
            if nup == 1:
                cx, cy = g.phys_w / 2, g.phys_h / 2
            else:
                cx, cy = (cell[0] + cell[2]) / 2, (cell[1] + cell[3]) / 2
            dw, dh = pw_px, ph_px
        else:
            s = (min if scaling == "FIT" else max)(cw / pw_px, ch / ph_px)
            dw, dh = pw_px * s, ph_px * s
            cx, cy = (cell[0] + cell[2]) / 2, (cell[1] + cell[3]) / 2
        full = (cx - dw / 2, cy - dh / 2, cx + dw / 2, cy + dh / 2)
        vis = _intersect(full, cell)
        if vis is None:
            continue
        crop = ((vis[0] - full[0]) / dw, (vis[1] - full[1]) / dh,
                (vis[2] - full[0]) / dw, (vis[3] - full[1]) / dh)
        clipped = scaling == "ACTUAL" and (crop[0] > 1e-3 or crop[1] > 1e-3
                                           or crop[2] < 1 - 1e-3 or crop[3] < 1 - 1e-3)
        placements.append(Placement(page_index=page_idx, rotate=rot, crop=crop,
                                    dest=_transform_rect(vis, g, mirror, rotate180),
                                    flip_h=mirror != rotate180, flip_v=rotate180,
                                    clipped=clipped))
    # Image ops mirror the rect mapping in _transform_rect: 180° = H+V flip,
    # mirror = H flip, mirror+180° = V flip only.
    return SheetPlan(placements=placements,
                     cells=[_transform_rect(c, g, mirror, rotate180) for c in cells],
                     content=_transform_rect(content, g, mirror, rotate180),
                     printable=g.printable)
