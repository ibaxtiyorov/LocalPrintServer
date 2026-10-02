"""Server-side validation of print options submitted by the browser.

The browser can only pick from enumerated keys; numeric values are range-checked.
Raw DEVMODE / DriverData is never accepted from clients (spec §29).
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from . import catalog


@dataclass
class PrintOptions:
    paper: str = "A4"
    custom_w_mm: float | None = None
    custom_h_mm: float | None = None
    media: str = "PLAIN"
    quality: str = "STANDARD"
    color: str = "COLOR"
    orientation: str = "AUTO"
    scaling: str = "FIT"
    margins_mm: float = 0.0
    copies: int = 1
    collate: bool = True
    reverse: bool = False
    nup: int = 1
    borderless: bool = False
    mirror: bool = False
    rotate180: bool = False
    page_range: str = ""                             # single-document selection text
    # Multi-document jobs: per document {"page_range": "2,5-8", "pages": [1, 4, 5, 6, 7]}
    # (local 0-based pages, in print order). `pages` holds the resulting GLOBAL indices
    # into the concatenated sequence of all documents - only selected pages appear.
    documents: list[dict] = field(default_factory=list)
    pages: list[int] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PrintOptions":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def paper_dims_mm(self) -> tuple[float, float]:
        return catalog.paper_dims_mm(self.paper, self.custom_w_mm, self.custom_h_mm)

    def summary(self) -> str:
        p = (f"{self.custom_w_mm:g}x{self.custom_h_mm:g}mm" if self.paper == catalog.CUSTOM_PAPER_KEY
             else self.paper)
        return (f"{p} {self.media} {self.quality} {self.color} {self.orientation} {self.scaling}"
                f" m={self.margins_mm:g} x{self.copies} collate={int(self.collate)}"
                f" rev={int(self.reverse)} nup={self.nup} bl={int(self.borderless)}"
                f" mirror={int(self.mirror)} r180={int(self.rotate180)}")


class OptionsError(Exception):
    def __init__(self, errors: dict[str, str]):
        super().__init__("invalid print options")
        self.errors = errors


_BOOL_TRUE = {True, 1, "1", "true", "on", "yes"}
_BOOL_FALSE = {False, 0, "0", "false", "off", "no", "", None}
_RANGE_RE = re.compile(r"^\s*(\d+)\s*(?:-\s*(\d*)\s*)?$")


def _to_bool(v, errors, name):
    if v in _BOOL_TRUE or (isinstance(v, str) and v.lower() in _BOOL_TRUE):
        return True
    if v in _BOOL_FALSE or (isinstance(v, str) and v.lower() in _BOOL_FALSE):
        return False
    errors[name] = "err.invalid_value"
    return False


MAX_SELECTED_PAGES = 5000


def parse_page_range(text: str, page_count: int) -> list[int]:
    """'2,5-8,15,20-' -> sorted unique 0-based pages. Empty = all pages. Raises ValueError.

    A selection is a SET of pages printed in document order (reverse order is a
    separate option), so typed ranges and clicked thumbnails always agree."""
    text = (text or "").strip()
    if not text:
        return list(range(page_count))
    if len(text) > 200:
        raise ValueError("too long")
    out: list[int] = []
    for part in text.split(","):
        if not part.strip():
            continue
        m = _RANGE_RE.match(part)
        if not m:
            raise ValueError("syntax")
        a = int(m.group(1))
        if m.group(2) is None and "-" not in part:
            b = a
        else:
            b = int(m.group(2)) if m.group(2) else page_count
        if a < 1 or b < a or b > page_count:
            raise ValueError("out of range")
        out.extend(range(a - 1, b))
        if len(out) > MAX_SELECTED_PAGES * 4:
            raise ValueError("too many")
    out = sorted(set(out))
    if not out:
        raise ValueError("empty")
    if len(out) > MAX_SELECTED_PAGES:
        raise ValueError("too many")
    return out


def format_page_range(pages: list[int]) -> str:
    """[0,1,2,4,7,8] -> '1-3,5,8-9' (input 0-based, output 1-based)."""
    out, run = [], []
    for p in sorted(set(pages)):
        if run and p == run[-1] + 1:
            run.append(p)
            continue
        if run:
            out.append(f"{run[0] + 1}-{run[-1] + 1}" if len(run) > 1 else str(run[0] + 1))
        run = [p]
    if run:
        out.append(f"{run[0] + 1}-{run[-1] + 1}" if len(run) > 1 else str(run[0] + 1))
    return ",".join(out)


def validate_options(raw: dict, settings: dict, borderless_papers: dict[str, int],
                     available_papers: set[str], page_count: int | list[int],
                     doc_ranges: list[str] | None = None) -> PrintOptions:
    """Validate raw browser input. Raises OptionsError({field: i18n_key}).

    `page_count` is an int for a single document (selection in raw["page_range"]) or a
    list of page counts for a multi-document job (selections in `doc_ranges`)."""
    if not isinstance(raw, dict):
        raise OptionsError({"_": "err.invalid_value"})
    e: dict[str, str] = {}
    o = PrintOptions()

    paper = raw.get("paper", settings["default_paper"])
    if paper == catalog.CUSTOM_PAPER_KEY:
        o.paper = paper
        try:
            w = round(float(raw.get("custom_w_mm")), 1)
            h = round(float(raw.get("custom_h_mm")), 1)
        except (TypeError, ValueError):
            e["custom"] = "err.custom_size"
        else:
            if not (catalog.CUSTOM_MIN_W_MM <= w <= catalog.CUSTOM_MAX_W_MM
                    and catalog.CUSTOM_MIN_H_MM <= h <= catalog.CUSTOM_MAX_H_MM):
                e["custom"] = "err.custom_size"
            o.custom_w_mm, o.custom_h_mm = w, h
    elif paper in catalog.PAPER_BY_KEY:
        if paper not in available_papers:
            e["paper"] = "err.paper_unavailable"
        o.paper = paper
    else:
        e["paper"] = "err.invalid_value"

    def pick(name, choices, default):
        v = raw.get(name, default)
        if v not in choices:
            e[name] = "err.invalid_value"
            return default
        return v

    o.media = pick("media", catalog.MEDIA_BY_KEY, settings["default_media"])
    o.quality = pick("quality", catalog.QUALITY, settings["default_quality"])
    o.color = pick("color", catalog.COLOR, settings["default_color"])
    o.orientation = pick("orientation", catalog.ORIENTATION_CHOICES,
                         settings["default_orientation"])
    o.scaling = pick("scaling", catalog.SCALING_CHOICES, settings["default_scaling"])

    try:
        o.margins_mm = round(float(raw.get("margins_mm", settings["default_margins_mm"])), 1)
        if not 0 <= o.margins_mm <= 50:
            raise ValueError
    except (TypeError, ValueError):
        e["margins_mm"] = "err.margins"
        o.margins_mm = 0.0

    try:
        o.copies = int(raw.get("copies", 1))
        if not 1 <= o.copies <= settings["max_copies"]:
            raise ValueError
    except (TypeError, ValueError):
        e["copies"] = "err.copies"
        o.copies = 1

    try:
        o.nup = int(raw.get("nup", 1))
        if o.nup not in catalog.NUP_CHOICES:
            raise ValueError
    except (TypeError, ValueError):
        e["nup"] = "err.invalid_value"
        o.nup = 1

    o.collate = _to_bool(raw.get("collate", True), e, "collate")
    o.reverse = _to_bool(raw.get("reverse", False), e, "reverse")
    o.borderless = _to_bool(raw.get("borderless", False), e, "borderless")
    o.mirror = _to_bool(raw.get("mirror", False), e, "mirror")
    o.rotate180 = _to_bool(raw.get("rotate180", False), e, "rotate180")

    if "margins_mm" not in e and "custom" not in e and "paper" not in e and not o.borderless:
        # Margins on both sides must leave room on the sheet (smallest side decides).
        if 2 * o.margins_mm >= min(o.paper_dims_mm()) - 10:
            e["margins_mm"] = "err.margins"

    if o.nup > 1 and o.scaling == "ACTUAL":
        e["scaling"] = "err.nup_actual"
    if o.borderless:
        if not settings["borderless_enabled"] or o.paper not in borderless_papers:
            e["borderless"] = "err.borderless_unavailable"
        o.margins_mm = 0.0

    if isinstance(page_count, int):
        counts, ranges, keys = [page_count], [raw.get("page_range", "")], ["page_range"]
    else:
        counts = list(page_count)
        ranges = list(doc_ranges or [""] * len(counts))
        keys = [f"page_range.{i}" for i in range(len(counts))]
    offset = 0
    for count, text, key in zip(counts, ranges, keys):
        text = str(text or "").strip()
        try:
            local = parse_page_range(text, count)
        except ValueError:
            e[key] = "err.page_range"
            local = []
        o.documents.append({"page_range": text, "pages": local})
        o.pages.extend(offset + p for p in local)
        offset += count
    if len(o.pages) > MAX_SELECTED_PAGES:
        e.setdefault(keys[-1], "err.page_range")
    o.page_range = o.documents[0]["page_range"] if len(o.documents) == 1 else ""

    if e:
        raise OptionsError(e)
    return o
