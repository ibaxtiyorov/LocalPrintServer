
import pytest
from PIL import Image

from lps.documents import (DocumentError, ImageDocument, PdfDocument, detect_type, inspect_upload,
                           sanitize_filename)

from .conftest import make_pdf, make_png

ALL = ["pdf", "jpeg", "png", "bmp", "gif", "tiff", "webp"]


def test_detect_by_content_not_name():
    assert detect_type(make_png()[:64]) == "png"
    assert detect_type(b"%PDF-1.7\n...") == "pdf"
    assert detect_type(b"\xff\xd8\xff\xe0....") == "jpeg"
    assert detect_type(b"MZ\x90\x00 executable") is None


@pytest.mark.parametrize("raw,clean", [
    ("..\\..\\Windows\\system32\\evil.pdf", "evil.pdf"), ("../../etc/passwd", "passwd"),
    ("Отчёт 2024.pdf", "Отчёт 2024.pdf"), ("a<b>:c?.png", "a_b__c_.png"), ("", "document"),
    ("...", "document")])
def test_sanitize_filename(raw, clean):
    assert sanitize_filename(raw) == clean


def test_exe_renamed_pdf_rejected(tmp_path):
    p = tmp_path / "x.bin"
    p.write_bytes(b"MZ" + b"\x00" * 200)
    with pytest.raises(DocumentError) as e:
        inspect_upload(p, ALL, 100)
    assert e.value.code == "err.unsupported_type"


def test_type_not_allowed(tmp_path):
    p = tmp_path / "x.bin"
    p.write_bytes(make_png())
    with pytest.raises(DocumentError) as e:
        inspect_upload(p, ["pdf"], 100)
    assert e.value.code == "err.unsupported_type"


def test_truncated_image_rejected(tmp_path):
    p = tmp_path / "x.bin"
    p.write_bytes(make_png()[:100])
    with pytest.raises(DocumentError):
        inspect_upload(p, ALL, 100)


def test_exif_orientation_applied(tmp_path):
    im = Image.new("RGB", (600, 400), "white")
    exif = im.getexif()
    exif[0x0112] = 6          # rotate 90° CW on display
    p = tmp_path / "photo.jpg"
    im.save(p, "JPEG", exif=exif.tobytes())
    doc = ImageDocument(p, "jpeg", 100)
    info = doc.page_info(0)
    assert info.height_mm > info.width_mm              # portrait after EXIF transpose
    rendered = doc.render_page(0, 10000, 10000)
    assert rendered.size == (400, 600)


def test_image_dpi_metadata_used_for_physical_size(tmp_path):
    p = tmp_path / "a.png"
    p.write_bytes(make_png(1181, 1181, dpi=(300, 300)))      # 100 mm at 300 dpi
    info = ImageDocument(p, "png").page_info(0)
    assert info.width_mm == pytest.approx(100, abs=0.1)


def test_alpha_composited_on_white(tmp_path):
    p = tmp_path / "t.png"
    Image.new("RGBA", (10, 10), (0, 0, 0, 0)).save(p)
    im = ImageDocument(p, "png").render_page(0, 10, 10)
    assert im.mode == "RGB" and im.getpixel((5, 5)) == (255, 255, 255)


def test_pdf_pages_sizes_and_render(tmp_path):
    p = tmp_path / "d.pdf"
    p.write_bytes(make_pdf(3))
    assert inspect_upload(p, ALL, 100) == ("pdf", 3)
    with PdfDocument(p, 150) as doc:
        info = doc.page_info(1)
        assert info.width_mm == pytest.approx(210, abs=0.5) and info.height_mm == pytest.approx(297, abs=0.5)
        im = doc.render_page(0, 300, 300)
        assert max(im.size) >= 300 and im.mode == "RGB"


def test_pdf_page_limit(tmp_path):
    p = tmp_path / "d.pdf"
    p.write_bytes(make_pdf(5))
    with pytest.raises(DocumentError) as e:
        inspect_upload(p, ALL, 4)
    assert e.value.code == "err.pdf_too_many_pages"


def test_encrypted_pdf_rejected(tmp_path):
    import pymupdf
    doc = pymupdf.open()
    doc.new_page()
    p = tmp_path / "enc.pdf"
    doc.save(str(p), encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="secret", owner_pw="owner")
    doc.close()
    with pytest.raises(DocumentError) as e:
        inspect_upload(p, ALL, 10)
    assert e.value.code == "err.pdf_encrypted"


def test_corrupt_pdf_rejected(tmp_path):
    p = tmp_path / "bad.pdf"
    p.write_bytes(b"%PDF-1.4\n garbage garbage")
    with pytest.raises(DocumentError):
        inspect_upload(p, ALL, 10)


def test_degenerate_pdf_page_box_is_handled(tmp_path):
    """MuPDF repairs empty page boxes (Letter) and clamps tiny ones to 1 pt; either way the
    document must be inspected and rendered without crashing."""
    import pymupdf
    doc = pymupdf.open()
    doc.new_page(width=595, height=842)
    data = doc.tobytes()
    doc.close()
    assert b"/MediaBox[0 0 595 842]" in data
    for box in (b"/MediaBox[0 0 0 00000]", b"/MediaBox[0 0 1 00001]"):   # same byte length
        p = tmp_path / "odd.pdf"
        p.write_bytes(data.replace(b"/MediaBox[0 0 595 842]", box))
        assert inspect_upload(p, ALL, 10) == ("pdf", 1)
        with PdfDocument(p) as d:
            info = d.page_info(0)
            assert info.width_mm > 0 and info.height_mm > 0
            assert d.render_page(0, 200, 200).size[0] >= 1
