"""Uploaded document handling: type detection, validation, page sources.

Uploads are identified by content (magic bytes), never by the client's filename
or MIME type. Files are only ever *parsed* (Pillow / PyMuPDF) — never executed.
"""
from __future__ import annotations

import bisect
import logging
import re
import threading
import unicodedata
from pathlib import Path

from PIL import Image, ImageOps

from .layout import MM_PER_INCH, PageInfo

log = logging.getLogger("lps.documents")

# Decompression-bomb guard: Pillow raises DecompressionBombError above 2x this.
Image.MAX_IMAGE_PIXELS = 90_000_000
MAX_IMAGE_PIXELS_HARD = 120_000_000
# Upper bound on pixels of a single rendered PDF page (memory safety).
MAX_RENDER_PIXELS = 60_000_000
# MuPDF is not thread-safe: every PyMuPDF call (worker and preview threads) runs under this lock.
MUPDF_LOCK = threading.RLock()

try:
    import pymupdf  # PyMuPDF >= 1.24
except ImportError:  # pragma: no cover - older installs expose only "fitz"
    try:
        import fitz as pymupdf
    except ImportError:
        pymupdf = None


class DocumentError(Exception):
    """User-facing document problem. `code` is an i18n key."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def pdf_available() -> bool:
    return pymupdf is not None


def detect_type(head: bytes) -> str | None:
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head.startswith(b"BM"):
        return "bmp"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if b"%PDF-" in head[:1024]:
        return "pdf"
    return None


_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')


def sanitize_filename(name: str | None) -> str:
    """Display-only name. Storage always uses a random server-side name."""
    name = unicodedata.normalize("NFC", str(name or ""))
    name = re.split(r"[\\/]", name)[-1]
    name = _UNSAFE.sub("_", name).strip().strip(".")
    if len(name) > 150:
        stem, dot, ext = name.rpartition(".")
        name = (stem[:140] + "." + ext[:9]) if dot and len(ext) <= 9 else name[:150]
    return name or "document"


class Document:
    doc_type: str
    page_count: int

    def page_info(self, index: int) -> PageInfo:
        raise NotImplementedError

    def render_page(self, index: int, max_w: int, max_h: int) -> Image.Image:
        """RGB image of the page, no larger than needed for a max_w x max_h box."""
        raise NotImplementedError

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _to_rgb(im: Image.Image) -> Image.Image:
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.getchannel("A"))
        return bg
    if im.mode == "I;16" or im.mode.startswith("I;16"):
        im = im.point(lambda v: v * (1 / 256)).convert("L")
    return im.convert("RGB") if im.mode != "RGB" else im


class ImageDocument(Document):
    def __init__(self, path: Path, doc_type: str, default_dpi: int = 96):
        self.path = Path(path)
        self.doc_type = doc_type
        self.page_count = 1
        self._default_dpi = default_dpi
        self._image: Image.Image | None = None
        try:
            with Image.open(self.path) as im:
                w, h = im.size
                if w * h > MAX_IMAGE_PIXELS_HARD:
                    raise DocumentError("err.image_too_large")
                dpi = im.info.get("dpi")
                # Orientation tag 5-8 swaps width/height.
                orient = im.getexif().get(0x0112, 1)
        except DocumentError:
            raise
        except (Image.DecompressionBombError, Image.DecompressionBombWarning):
            raise DocumentError("err.image_too_large")
        except Exception as e:
            raise DocumentError("err.corrupt_file", str(e))
        if orient in (5, 6, 7, 8):
            w, h = h, w
        self.px_w, self.px_h = w, h
        try:
            dx, dy = (float(dpi[0]), float(dpi[1])) if dpi else (0.0, 0.0)
        except (TypeError, ValueError, IndexError):
            dx = dy = 0.0
        # Ignore absent/implausible DPI metadata.
        if not (50 <= dx <= 2400 and 50 <= dy <= 2400):
            dx = dy = float(default_dpi)
        if orient in (5, 6, 7, 8):
            dx, dy = dy, dx
        self.dpi_x, self.dpi_y = dx, dy

    def _load(self) -> Image.Image:
        if self._image is None:
            try:
                with Image.open(self.path) as im:
                    im.seek(0)            # first frame of GIF / multi-page TIFF
                    im = ImageOps.exif_transpose(im)
                    self._image = _to_rgb(im)
                    self._image.load()
            except (Image.DecompressionBombError, Image.DecompressionBombWarning):
                raise DocumentError("err.image_too_large")
            except Exception as e:
                raise DocumentError("err.corrupt_file", str(e))
        return self._image

    def page_info(self, index: int) -> PageInfo:
        return PageInfo(self.px_w / self.dpi_x * MM_PER_INCH, self.px_h / self.dpi_y * MM_PER_INCH)

    def render_page(self, index: int, max_w: int, max_h: int) -> Image.Image:
        im = self._load()
        max_w, max_h = max(1, int(max_w)), max(1, int(max_h))
        if im.width > max_w or im.height > max_h:
            # Never upscale in software: GDI stretches to the destination rect.
            s = min(max_w / im.width, max_h / im.height)
            return im.resize((max(1, round(im.width * s)), max(1, round(im.height * s))),
                             Image.LANCZOS)
        return im

    def close(self):
        self._image = None


class PdfDocument(Document):
    doc_type = "pdf"

    def __init__(self, path: Path, render_dpi: int = 300):
        if pymupdf is None:
            raise DocumentError("err.pdf_unavailable")
        self.path = Path(path)
        self.render_dpi = render_dpi
        with MUPDF_LOCK:
            try:
                self._doc = pymupdf.open(str(self.path), filetype="pdf")
            except Exception as e:
                raise DocumentError("err.pdf_corrupt", str(e))
            if self._doc.needs_pass or self._doc.is_encrypted:
                self._doc.close()
                raise DocumentError("err.pdf_encrypted")
            self.page_count = self._doc.page_count
            if self.page_count < 1:
                self._doc.close()
                raise DocumentError("err.pdf_empty")

    def page_info(self, index: int) -> PageInfo:
        with MUPDF_LOCK:
            try:
                r = self._doc[index].rect  # already accounts for /Rotate
            except Exception as e:
                raise DocumentError("err.pdf_render", f"page {index + 1}: {e}")
        if r.width < 1 or r.height < 1:    # degenerate page box (malformed PDF)
            raise DocumentError("err.pdf_render", f"page {index + 1}: empty page size")
        return PageInfo(r.width / 72 * MM_PER_INCH, r.height / 72 * MM_PER_INCH)

    def render_page(self, index: int, max_w: int, max_h: int) -> Image.Image:
        with MUPDF_LOCK:
            return self._render(index, max_w, max_h)

    def _render(self, index: int, max_w: int, max_h: int) -> Image.Image:
        page = self._doc[index]
        r = page.rect
        # Resolution needed to fill the destination box, capped by render_dpi and memory.
        need = max(max_w / (r.width / 72), max_h / (r.height / 72))
        dpi = min(float(self.render_dpi), need)
        pixels = (r.width / 72 * dpi) * (r.height / 72 * dpi)
        if pixels > MAX_RENDER_PIXELS:
            dpi *= (MAX_RENDER_PIXELS / pixels) ** 0.5
        dpi = max(dpi, 10.0)
        try:
            pix = page.get_pixmap(matrix=pymupdf.Matrix(dpi / 72, dpi / 72), alpha=False,
                                  colorspace=pymupdf.csRGB)
            return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        except Exception as e:
            raise DocumentError("err.pdf_render", f"page {index + 1}: {e}")

    def close(self):
        with MUPDF_LOCK:
            try:
                self._doc.close()
            except Exception:
                pass


# Word/Excel uploads are stored as the PDF they were converted to (see lps.office).
OFFICE_TYPES = ("docx", "xlsx", "doc", "xls")


def render_type(doc_type: str) -> str:
    """File format actually stored/rendered for a document type."""
    return "pdf" if doc_type in OFFICE_TYPES else doc_type


def open_document(path: Path, doc_type: str, *, pdf_render_dpi: int = 300,
                  default_image_dpi: int = 96) -> Document:
    if render_type(doc_type) == "pdf":
        return PdfDocument(path, pdf_render_dpi)
    return ImageDocument(path, doc_type, default_image_dpi)


class CompositeDocument(Document):
    """Several documents concatenated into ONE page sequence.

    Global page index = offset of the document + its local page index. The layout,
    preview and print worker only ever see global indices, so multi-document jobs use
    exactly the same pipeline as single files."""
    doc_type = "composite"

    def __init__(self, docs: list[Document]):
        if not docs:
            raise DocumentError("err.no_file")
        self.docs = docs
        self.offsets, total = [], 0
        for d in docs:
            self.offsets.append(total)
            total += d.page_count
        self.page_count = total

    def locate(self, index: int) -> tuple[int, int]:
        """Global page index -> (document number, local page index)."""
        if not 0 <= index < self.page_count:
            raise IndexError(index)
        d = bisect.bisect_right(self.offsets, index) - 1
        return d, index - self.offsets[d]

    def page_info(self, index: int) -> PageInfo:
        d, i = self.locate(index)
        return self.docs[d].page_info(i)

    def render_page(self, index: int, max_w: int, max_h: int) -> Image.Image:
        d, i = self.locate(index)
        return self.docs[d].render_page(i, max_w, max_h)

    def close(self):
        for d in self.docs:
            d.close()


def inspect_upload(path: Path, allowed_types: list[str], max_pdf_pages: int) -> tuple[str, int]:
    """Validate a stored upload. Returns (doc_type, page_count) or raises DocumentError."""
    with open(path, "rb") as fh:
        head = fh.read(1024)
    if not head:
        raise DocumentError("err.empty_file")
    doc_type = detect_type(head)
    if doc_type is None or doc_type not in allowed_types:
        raise DocumentError("err.unsupported_type")
    if doc_type == "pdf":
        with PdfDocument(path) as doc:
            if doc.page_count > max_pdf_pages:
                raise DocumentError("err.pdf_too_many_pages")
            # Parse the first page to catch broken content early.
            doc.render_page(0, 64, 64)
            return doc_type, doc.page_count
    try:
        with Image.open(path) as im:
            im.verify()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise DocumentError("err.image_too_large")
    except Exception as e:
        raise DocumentError("err.corrupt_file", str(e))
    ImageDocument(path, doc_type)   # size checks
    return doc_type, 1


def open_parts(parts: list[dict], base_dir: Path, *, pdf_render_dpi: int = 300,
               default_image_dpi: int = 96) -> CompositeDocument:
    """Open every part of a job/preview as one CompositeDocument (closes on failure)."""
    docs: list[Document] = []
    try:
        for part in parts:
            path = Path(base_dir) / part["stored_name"]
            if not part.get("stored_name") or not path.exists():
                raise DocumentError("err.file_missing")
            docs.append(open_document(path, part["doc_type"], pdf_render_dpi=pdf_render_dpi,
                                      default_image_dpi=default_image_dpi))
        return CompositeDocument(docs)
    except BaseException:
        for d in docs:
            d.close()
        raise
