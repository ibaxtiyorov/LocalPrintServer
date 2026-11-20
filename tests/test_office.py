"""Word/Excel support with simulated conversion (no Office software needed).

The real engines (Microsoft Office COM, LibreOffice) are exercised by intercepting
the process launch, so the exact command line, isolation, timeout handling and
cleanup are verified without Office being installed."""
import io
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from lps import jobs as J
from lps import office
from lps.office import OfficeConverter, OfficeError, detect_office, validate_office
from lps.office_samples import make_docx, make_xlsx

from .conftest import make_color_pdf, make_ole, upload, upload_ok, wait_status


def write(tmp_path, name, data):
    p = tmp_path / name
    p.write_bytes(data)
    return p


def det(tmp_path, data):
    p = write(tmp_path, "x.bin", data)
    return detect_office(p, data[:1024])


# ------------------------------------------------------------------ detection
def test_detect_by_content(tmp_path):
    assert det(tmp_path, make_docx(1)) == "docx"
    assert det(tmp_path, make_xlsx(1)) == "xlsx"
    assert det(tmp_path, make_ole("WordDocument")) == "doc"
    assert det(tmp_path, make_ole("Workbook")) == "xls"
    assert det(tmp_path, make_ole("Book")) == "xls"
    assert det(tmp_path, make_ole("EncryptedPackage")) == "encrypted"
    assert det(tmp_path, make_ole("PowerPoint Document")) is None
    assert det(tmp_path, b"%PDF-1.4 ...") is None


def test_plain_zip_is_not_office(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("readme.txt", "hello")
    assert det(tmp_path, buf.getvalue()) is None


def test_ole_name_in_document_text_is_not_a_false_positive(tmp_path):
    data = bytearray(make_ole("Book"))                    # an Excel file...
    text = "WordDocument".encode("utf-16-le")             # ...mentioning WordDocument in text
    data[1500:1500 + len(text)] = text
    assert det(tmp_path, bytes(data)) == "xls"


# ------------------------------------------------------------------ validation
def test_macro_enabled_rejected(tmp_path):
    p = write(tmp_path, "m.docx", make_docx(1, content_type_extra=
        '<Override PartName="/word/x.xml" ContentType="application/vnd.ms-word.document.macroEnabled.main+xml"/>'))
    with pytest.raises(OfficeError) as e:
        validate_office(p, "docx")
    assert e.value.code == "err.office_macros"


def test_external_template_rejected_but_hyperlinks_allowed(tmp_path):
    ext = ('<Relationship Id="r9" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
           'relationships/attachedTemplate" Target="\\\\attacker\\share\\t.dotx" TargetMode="External"/>')
    with pytest.raises(OfficeError) as e:
        validate_office(write(tmp_path, "t.docx", make_docx(1, extra_rels=ext)), "docx")
    assert e.value.code == "err.office_external"
    link = ('<Relationship Id="r8" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships/hyperlink" Target="https://example.com" TargetMode="External"/>')
    validate_office(write(tmp_path, "h.docx", make_docx(1, extra_rels=link)), "docx")


def test_encrypted_rejected(tmp_path):
    with pytest.raises(OfficeError) as e:
        validate_office(write(tmp_path, "e.bin", make_ole("EncryptedPackage")), "encrypted")
    assert e.value.code == "err.office_encrypted"


def test_zip_bomb_and_unsafe_part_names_rejected(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", "<w/>")
        z.writestr("word/media/big.bin", b"\0" * (20 * 1024 * 1024))     # ~20 MB of zeros
    with pytest.raises(OfficeError):
        validate_office(write(tmp_path, "bomb.docx", buf.getvalue()), "docx")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", "<w/>")
        z.writestr("../../evil.txt", "x")
    with pytest.raises(OfficeError):
        validate_office(write(tmp_path, "trav.docx", buf.getvalue()), "docx")


# ------------------------------------------------------------------ end to end (simulated)
def test_docx_upload_preview_and_print(alice, svc):
    up = upload_ok(alice, make_docx(3), "Отчёт.docx")
    assert (up["doc_type"], up["pages"], up["converted"]) == ("docx", 3, True)
    row = svc.db.one("SELECT stored_name FROM uploads WHERE id = ?", (up["id"],))
    assert row["stored_name"].endswith(".pdf")
    stored = list(svc.cfg.uploads_dir.iterdir())
    assert [p.suffix for p in stored] == [".pdf"]                     # original removed
    assert stored[0].read_bytes().startswith(b"%PDF")
    pv = alice.post("/api/preview", json={"documents": [{"upload_id": up["id"], "page_range": "1,3"}],
                                          "options": {"nup": 2}}).get_json()
    assert pv["ok"] and pv["meta"]["order"] == [[[1, 1], [1, 3]]]
    r = alice.post("/api/jobs", json={"documents": [{"upload_id": up["id"], "page_range": "2-3"}],
                                      "options": {"color": "MONO"}})
    jid = r.get_json()["job"]["id"]
    job = wait_status(svc, jid, {J.COMPLETED})
    assert job["sheet_count"] == 2 and job["doc_type"] == "docx"
    part = svc.jobs.parts(job)[0]
    assert part["doc_type"] == "docx" and part["stored_name"].endswith(".pdf")


def test_xlsx_upload_one_page_per_sheet(alice, svc):
    up = upload_ok(alice, make_xlsx(3), "budget.xlsx")
    assert (up["doc_type"], up["pages"]) == ("xlsx", 3)
    r = alice.post("/api/jobs", json={"documents": [{"upload_id": up["id"]}],
                                      "options": {"orientation": "AUTO"}})
    job = wait_status(svc, r.get_json()["job"]["id"], {J.COMPLETED})
    assert job["sheet_count"] == 3


def test_legacy_doc_xls_with_word_and_pdf_in_one_job(alice, svc):
    doc = upload_ok(alice, make_ole("WordDocument"), "old.doc")
    xls = upload_ok(alice, make_ole("Workbook"), "old.xls")
    pdf = upload_ok(alice, make_color_pdf(2), "scan.pdf")
    assert (doc["doc_type"], xls["doc_type"]) == ("doc", "xls")
    r = alice.post("/api/jobs", json={"documents": [{"upload_id": x["id"]} for x in (pdf, doc, xls)]})
    job = wait_status(svc, r.get_json()["job"]["id"], {J.COMPLETED})
    assert job["sheet_count"] == 4 and job["doc_type"] == "multi"


def test_rejected_office_files_give_clear_errors(alice, svc):
    r = upload(alice, make_ole("EncryptedPackage"), "secret.docx")
    assert r.status_code == 400 and r.get_json()["error"] == "err.office_encrypted"
    ext = ('<Relationship Id="r9" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
           'relationships/oleObject" Target="file:///c:/x.xlsx" TargetMode="External"/>')
    r = upload(alice, make_docx(1, extra_rels=ext), "linked.docx")
    assert r.get_json()["error"] == "err.office_external"
    assert not list(svc.cfg.uploads_dir.iterdir())                 # nothing left behind


def test_office_type_disabled_by_admin(alice, svc):
    svc.settings.update({"allowed_types": ["pdf", "png", "jpeg"]}, "test")
    r = upload(alice, make_docx(1), "a.docx")
    assert r.status_code == 400 and r.get_json()["error"] == "err.unsupported_type"


def test_converter_unavailable_is_clear_and_pdf_still_works(alice, svc):
    svc.office.engine_setting = "none"
    r = upload(alice, make_docx(1), "a.docx")
    assert r.status_code == 503 and r.get_json()["error"] == "err.office_unavailable"
    cat = alice.get("/api/catalog").get_json()
    assert "docx" not in cat["allowed_types"] and "docx" in cat["office_unavailable"]
    upload_ok(alice, make_color_pdf(1), "fine.pdf")


def test_converted_page_limit(alice, svc):
    svc.settings.update({"max_pdf_pages": 2}, "test")
    r = upload(alice, make_docx(3), "long.docx")
    assert r.get_json()["error"] == "err.pdf_too_many_pages"
    assert not list(svc.cfg.uploads_dir.iterdir())


def test_admin_office_test_and_status(admin, svc):
    res = admin.post("/admin/api/office-test", json={}).get_json()["results"]
    assert [(r["kind"], r["ok"], r["pages"]) for r in res] == [("docx", True, 2), ("xlsx", True, 2)]
    page = admin.get("/admin/printer").get_data(as_text=True)
    assert "Word / Excel conversion" in page
    assert list((svc.cfg.data_dir / "convert_tmp").iterdir()) == []   # temp files cleaned


# ------------------------------------------------------------------ engines
def make_conv(tmp_path, engine, **extra):
    from lps.config import Config
    return OfficeConverter(Config({"office_converter": engine, "libreoffice_path": "",
                                   "office_timeout_seconds": 5, "data_dir": str(tmp_path), **extra}))


def test_engine_selection(tmp_path, monkeypatch):
    monkeypatch.setattr(office, "_msoffice_installed", lambda: {"word": True, "excel": False})
    monkeypatch.setattr(office, "_find_libreoffice", lambda cfg: None)
    c = make_conv(tmp_path, "auto")
    assert c.engine_for("docx") == ("msoffice", "")
    assert c.engine_for("xlsx") == (None, "office.reason.none_installed")
    assert make_conv(tmp_path, "msoffice").engine_for("xls") == (None, "office.reason.no_excel")
    monkeypatch.setattr(office, "_find_libreoffice", lambda cfg: r"C:\LO\soffice.exe")
    assert c.engine_for("xlsx") == ("libreoffice", "")
    assert make_conv(tmp_path, "libreoffice").engine_for("docx") == ("libreoffice", "")
    assert make_conv(tmp_path, "none").engine_for("docx") == (None, "office.reason.disabled")


class FakePopen:
    """Stands in for the converter process: records the command, writes a PDF."""
    calls = []
    hang = False

    def __init__(self, cmd, **kw):
        FakePopen.calls.append((cmd, kw))
        self.cmd, self.kw, self.pid, self.returncode = cmd, kw, 4242, 0
        self.killed = False

    def communicate(self, timeout=None):
        if FakePopen.hang and not self.killed:
            self.killed = True
            raise subprocess.TimeoutExpired(self.cmd, timeout)
        if "--convert-to" in self.cmd:                         # LibreOffice
            out = Path(self.cmd[self.cmd.index("--outdir") + 1]) / "input.pdf"
        else:                                                  # office_helper
            out = Path(self.cmd[6])
        out.write_bytes(make_color_pdf(1))
        return b"", None


@pytest.fixture()
def fake_popen(monkeypatch):
    FakePopen.calls, FakePopen.hang = [], False
    monkeypatch.setattr(office.subprocess, "Popen", FakePopen)
    killed = []
    monkeypatch.setattr(office, "_kill_tree", lambda pid: killed.append(pid))
    FakePopen.markers = []
    monkeypatch.setattr(office, "_kill_by_marker", lambda m: FakePopen.markers.append(m))
    return killed


def test_libreoffice_command_is_isolated_and_shell_free(tmp_path, monkeypatch, fake_popen):
    monkeypatch.setattr(office, "_find_libreoffice", lambda cfg: r"C:\LO\soffice.exe")
    c = make_conv(tmp_path, "libreoffice")
    src = write(tmp_path, "evil & del x ; calc.docx", make_docx(1))   # hostile-looking name
    out = tmp_path / "out.pdf"
    c.convert(src, "docx", out)
    cmd, kw = FakePopen.calls[0]
    assert kw["shell"] is False and isinstance(cmd, list)
    assert cmd[0] == r"C:\LO\soffice.exe" and "--headless" in cmd and "--norestore" in cmd
    assert Path(cmd[-1]).name == "input.docx"                          # fixed safe name
    assert not any("evil" in a or "&" in a for a in cmd)
    profile = [a for a in cmd if a.startswith("-env:UserInstallation=file:")]
    folder = office.cwd_marker(cmd)
    assert profile and f"lps-lo-{folder}" in profile[0]                # throw-away short profile
    assert out.read_bytes().startswith(b"%PDF")
    assert list((tmp_path / "convert_tmp").iterdir()) == []            # temp folder removed


def test_msoffice_runs_in_helper_process(tmp_path, monkeypatch, fake_popen):
    monkeypatch.setattr(office, "_msoffice_installed", lambda: {"word": True, "excel": True})
    c = make_conv(tmp_path, "msoffice")
    c.convert(write(tmp_path, "a.xlsx", make_xlsx(1)), "xlsx", tmp_path / "o.pdf")
    cmd, kw = FakePopen.calls[0]
    assert cmd[:5] == [sys.executable, "-s", "-m", "lps.office_helper", "excel"]
    assert Path(cmd[5]).name == "input.xlsx" and kw["shell"] is False


def test_timeout_kills_converter_and_cleans_up(tmp_path, monkeypatch, fake_popen):
    monkeypatch.setattr(office, "_find_libreoffice", lambda cfg: r"C:\LO\soffice.exe")
    FakePopen.hang = True
    c = make_conv(tmp_path, "libreoffice")
    with pytest.raises(OfficeError) as e:
        c.convert(write(tmp_path, "a.docx", make_docx(1)), "docx", tmp_path / "o.pdf")
    assert e.value.code == "err.office_timeout"
    assert fake_popen == [4242]                                        # process tree killed
    folder = office.cwd_marker(FakePopen.calls[0][0])
    assert len(folder) == 16 and FakePopen.markers[0] == folder         # orphans of THIS run
    assert list((tmp_path / "convert_tmp").iterdir()) == []
    assert not (tmp_path / "o.pdf").exists()


def test_failed_conversion_reports_and_cleans(tmp_path, monkeypatch, fake_popen):
    monkeypatch.setattr(office, "_find_libreoffice", lambda cfg: r"C:\LO\soffice.exe")

    class Failing(FakePopen):
        def communicate(self, timeout=None):
            self.returncode = 1
            return b"source file could not be loaded", None
    monkeypatch.setattr(office.subprocess, "Popen", Failing)
    c = make_conv(tmp_path, "libreoffice")
    with pytest.raises(OfficeError) as e:
        c.convert(write(tmp_path, "a.docx", make_docx(1)), "docx", tmp_path / "o.pdf")
    assert e.value.code == "err.office_failed" and "could not be loaded" in e.value.detail
    assert FakePopen.markers and len(FakePopen.markers[0]) == 16
    assert list((tmp_path / "convert_tmp").iterdir()) == []


def test_locked_folder_is_retried_then_left_for_later(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(office, "_kill_by_marker", lambda m: calls.append(m))
    monkeypatch.setattr(office.time, "sleep", lambda s: None)
    monkeypatch.setattr(office.shutil, "rmtree", lambda p, ignore_errors=False: None)  # "locked"
    d = tmp_path / "abc"
    d.mkdir()
    assert office._remove_dir(d, marker="abc") is False
    assert calls == ["abc"]                       # tried to kill the converter holding it


def test_old_folders_swept_before_conversion(tmp_path):
    import os
    import time
    c = make_conv(tmp_path, "simulated")
    old = tmp_path / "convert_tmp" / "old"
    new = tmp_path / "convert_tmp" / "new"
    old.mkdir(), new.mkdir()
    os.utime(old, (time.time() - 3600, time.time() - 3600))
    c.convert(write(tmp_path, "a.docx", make_docx(1)), "docx", tmp_path / "o.pdf")
    assert not old.exists() and new.exists()     # recent folder may belong to a running job


def test_leftover_temp_folders_swept_at_start(tmp_path):
    stale = tmp_path / "convert_tmp" / "abandoned"
    stale.mkdir(parents=True)
    (stale / "input.docx").write_bytes(b"x")
    make_conv(tmp_path, "simulated")
    assert not stale.exists()


def test_folders_beyond_max_path_are_removed(tmp_path):
    import os
    base = tmp_path / ("d" * 40)
    deep = os.path.join(office._long_path(base), *(["x" * 60] * 4))   # > 260 characters
    os.makedirs(deep)
    with open(os.path.join(deep, "backenddb.xml"), "w") as fh:
        fh.write("x")
    assert len(deep) > 260
    assert office._remove_dir(base) is True and not base.exists()


@pytest.mark.skipif(office._find_libreoffice("") is None, reason="LibreOffice not installed")
def test_real_libreoffice_conversion(tmp_path):
    """Runs only where LibreOffice is installed (e.g. the print server after setup)."""
    import tempfile
    from lps.documents import PdfDocument
    c = make_conv(tmp_path, "libreoffice", office_timeout_seconds=180)
    for kind, data, pages in (("docx", make_docx(3), 3), ("xlsx", make_xlsx(2), 2)):
        out = tmp_path / f"{kind}.pdf"
        c.convert(write(tmp_path, f"in.{kind}", data), kind, out)
        with PdfDocument(out) as d:
            assert d.page_count == pages
    assert list((tmp_path / "convert_tmp").iterdir()) == []
    assert not list(Path(tempfile.gettempdir()).glob("lps-lo-*"))


@pytest.mark.skipif(not all(office._msoffice_installed().values()),
                    reason="Microsoft Word/Excel not installed")
def test_real_microsoft_office_conversion(tmp_path):
    """Runs only where Word and Excel are installed. Uses a private invisible instance."""
    from lps.documents import PdfDocument
    c = make_conv(tmp_path, "msoffice", office_timeout_seconds=240)
    for kind, data, pages in (("docx", make_docx(3), 3), ("xlsx", make_xlsx(2), 2)):
        out = tmp_path / f"{kind}.pdf"
        c.convert(write(tmp_path, f"in.{kind}", data), kind, out)
        with PdfDocument(out) as d:
            assert d.page_count == pages
    assert list((tmp_path / "convert_tmp").iterdir()) == []
