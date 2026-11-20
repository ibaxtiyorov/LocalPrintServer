"""Word / Excel support: content validation and conversion to PDF.

Office files are never sent to the printer driver. They are converted once, at
upload time, to PDF; from then on they follow the normal PDF path (PyMuPDF
render -> layout -> preview -> queue -> print worker -> GDI), so every print
setting, page selection and N-up behaves exactly as for PDFs.

Conversion engines (config.json "office_converter"):
  "msoffice"    Microsoft Word / Excel via COM automation (best layout fidelity).
  "libreoffice" LibreOffice headless (soffice.exe), path in "libreoffice_path".
  "auto"        Microsoft Office if installed, otherwise LibreOffice, otherwise none.
  "none"        Word/Excel uploads are rejected with a clear message.
  "simulated"   Development/tests only: builds a PDF without any Office software.

Security:
  * type decided by content (OOXML package parts / OLE2 directory entries), never by
    the client filename; macro-enabled, encrypted and externally-linked documents are
    rejected before any Office software opens them;
  * zip-bomb limits for OOXML packages;
  * each conversion runs in its own temp folder with a fixed input name
    ("input.docx"), as a separate process started with an argument list (no shell),
    with a timeout, and the folder is deleted afterwards;
  * Office is started invisibly with macros force-disabled and alerts off;
    LibreOffice runs headless with a throw-away profile.
"""
from __future__ import annotations

import logging
import os
import re
import secrets
import shutil
import tempfile
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

from .platform_info import windows_session_id

log = logging.getLogger("lps.office")

OFFICE_TYPES = ("docx", "xlsx", "doc", "xls")
WORD_TYPES = ("docx", "doc")
OLE_MAGIC = b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1"
ZIP_MAGIC = b"PK\x03\x04"

# OOXML package limits (zip-bomb protection)
MAX_ZIP_ENTRIES = 5000
MAX_ZIP_UNCOMPRESSED = 400 * 1024 * 1024
MAX_ZIP_RATIO = 200

LIBREOFFICE_DEFAULTS = (r"C:\Program Files\LibreOffice\program\soffice.exe",
                        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe")
ENGINES = ("auto", "msoffice", "libreoffice", "none", "simulated")


class OfficeError(Exception):
    """User-facing problem; `code` is an i18n key, `detail` goes to the log only."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


# ===================================================================== detection
def _ole_has_stream(data: bytes, name: str) -> bool:
    """True if an OLE2 directory entry with this exact name exists.

    Directory entries are 128 bytes, start on 128-byte boundaries after the 512-byte
    header, hold the UTF-16LE name in the first 64 bytes and its byte length (incl.
    terminator) at offset 64. Checking all three avoids false hits on document text.
    """
    enc = name.encode("utf-16-le")
    want_len = (len(name) + 1) * 2
    start = 0
    while True:
        pos = data.find(enc, start)
        if pos < 0:
            return False
        start = pos + 1
        if pos < 512 or (pos - 512) % 128:
            continue
        if data[pos + len(enc):pos + len(enc) + 2] != b"\x00\x00":
            continue
        if int.from_bytes(data[pos + 64:pos + 66], "little") == want_len:
            return True


def detect_office(path: Path, head: bytes) -> str | None:
    """Return docx/xlsx/doc/xls (or None) from the file CONTENT."""
    if head.startswith(ZIP_MAGIC):
        try:
            with zipfile.ZipFile(path) as z:
                names = set(z.namelist())
        except zipfile.BadZipFile:
            return None
        if "[Content_Types].xml" not in names:
            return None
        if "word/document.xml" in names:
            return "docx"
        if "xl/workbook.xml" in names:
            return "xlsx"
        return None
    if head.startswith(OLE_MAGIC):
        data = Path(path).read_bytes()
        if _ole_has_stream(data, "EncryptedPackage"):
            return "encrypted"
        if _ole_has_stream(data, "WordDocument"):
            return "doc"
        if _ole_has_stream(data, "Workbook") or _ole_has_stream(data, "Book"):
            return "xls"
    return None


_EXTERNAL_REL = re.compile(rb'<Relationship\b[^>]*\bTargetMode\s*=\s*"External"[^>]*>', re.I)
_REL_TYPE = re.compile(rb'\bType\s*=\s*"([^"]+)"', re.I)


def validate_office(path: Path, kind: str) -> None:
    """Reject files that are unsafe or impossible to convert. Raises OfficeError."""
    if kind == "encrypted":
        raise OfficeError("err.office_encrypted")
    if kind not in ("docx", "xlsx"):
        return                                  # legacy binary formats: engine settings protect
    try:
        with zipfile.ZipFile(path) as z:
            infos = z.infolist()
            if len(infos) > MAX_ZIP_ENTRIES:
                raise OfficeError("err.office_corrupt", "too many package parts")
            total = 0
            for i in infos:
                total += i.file_size
                if i.file_size > 1_000_000 and i.compress_size and i.file_size / i.compress_size > MAX_ZIP_RATIO:
                    raise OfficeError("err.office_corrupt", f"suspicious compression ratio in {i.filename}")
                if ".." in i.filename.replace("\\", "/").split("/") or i.filename.startswith(("/", "\\")):
                    raise OfficeError("err.office_corrupt", "unsafe part name")
            if total > MAX_ZIP_UNCOMPRESSED:
                raise OfficeError("err.office_corrupt", "package too large when unpacked")
            ctypes = z.read("[Content_Types].xml")
            if b"macroEnabled" in ctypes or "word/vbaProject.bin" in z.namelist() \
                    or "xl/vbaProject.bin" in z.namelist():
                raise OfficeError("err.office_macros")
            for name in z.namelist():
                if not name.endswith(".rels"):
                    continue
                rels = z.read(name)
                if len(rels) > 5 * 1024 * 1024:
                    raise OfficeError("err.office_corrupt", "relationship part too large")
                for m in _EXTERNAL_REL.finditer(rels):
                    t = _REL_TYPE.search(m.group(0))
                    rel_type = t.group(1).decode("ascii", "replace") if t else ""
                    if not rel_type.endswith("/hyperlink"):
                        # remote templates, linked images/objects, external data sources
                        raise OfficeError("err.office_external", f"{name}: {rel_type}")
    except zipfile.BadZipFile as e:
        raise OfficeError("err.office_corrupt", str(e))
    except KeyError as e:
        raise OfficeError("err.office_corrupt", str(e))


# ===================================================================== engines
def _msoffice_installed() -> dict[str, bool]:
    found = {"word": False, "excel": False}
    try:
        import winreg
        for app, progid in (("word", "Word.Application"), ("excel", "Excel.Application")):
            try:
                with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, progid + r"\CLSID"):
                    found[app] = True
            except OSError:
                pass
    except ImportError:
        pass
    return found


def _find_libreoffice(configured: str) -> str | None:
    candidates = [configured] if configured else list(LIBREOFFICE_DEFAULTS)
    for c in candidates:
        if c and Path(c).is_file():
            return str(Path(c))
    return None


def _kill_tree(pid: int):
    try:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, timeout=30,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        log.warning("could not kill process tree %s", pid, exc_info=True)


def _matching_pids(marker: str) -> list[int]:
    """PIDs of converter processes whose command line contains `marker` (COM objects are
    created and released inside this function, before COM is uninitialised)."""
    import win32com.client
    wmi = win32com.client.GetObject("winmgmts:\\\\.\\root\\cimv2")
    query = ("SELECT ProcessId, CommandLine FROM Win32_Process WHERE Name = 'soffice.bin'"
             " OR Name = 'soffice.exe' OR Name = 'python.exe' OR Name = 'pythonw.exe'")
    return [int(p.ProcessId) for p in wmi.ExecQuery(query)
            if marker.lower() in (p.CommandLine or "").lower() and int(p.ProcessId) != os.getpid()]


def _kill_by_marker(marker: str):
    """Kill converter processes whose command line contains `marker` (the random name of
    one conversion folder). Catches LibreOffice's soffice.bin, which can outlive a crashed
    soffice.exe launcher and is then no longer part of its process tree. Never touches a
    user's own Office/LibreOffice session (their command lines never contain the marker)."""
    if not marker:
        return
    try:
        import pythoncom
    except ImportError:
        return
    pids: list[int] = []
    pythoncom.CoInitialize()
    try:
        pids = _matching_pids(marker)
    except Exception:
        log.warning("could not look for leftover converter processes", exc_info=True)
    finally:
        pythoncom.CoUninitialize()
    for pid in pids:
        _kill_tree(pid)
        log.warning("killed leftover converter process %s", pid)


def _long_path(path: Path) -> str:
    """Extended-length form so deletion works beyond the 260-character MAX_PATH limit
    (LibreOffice profiles nest ~120 characters deep)."""
    p = str(Path(path).resolve())
    if os.name == "nt" and not p.startswith("\\\\?\\"):
        return "\\\\?\\UNC\\" + p[2:] if p.startswith("\\\\") else "\\\\?\\" + p
    return p


def _remove_dir(path: Path, marker: str | None = None) -> bool:
    """Delete a conversion folder; files may stay locked briefly while a converter exits."""
    for attempt in range(12):
        shutil.rmtree(_long_path(path), ignore_errors=True)
        if not path.exists():
            return True
        if attempt == 2 and marker:
            _kill_by_marker(marker)
        time.sleep(0.5)
    log.warning("could not remove conversion folder %s yet; it will be removed later", path)
    return False


def _kill_office_pids(pid_file: Path):
    """Kill the Word/Excel instance started by our helper (PIDs recorded by the helper)."""
    if not pid_file.exists():
        return
    for line in pid_file.read_text(encoding="ascii", errors="ignore").split():
        if line.isdigit():
            try:
                out = subprocess.run(["tasklist", "/FI", f"PID eq {line}", "/FO", "CSV", "/NH"],
                                     capture_output=True, text=True, timeout=30,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
                if re.search(r"(?i)\b(WINWORD|EXCEL)\.EXE\b", out):
                    _kill_tree(int(line))
                    log.warning("killed hung Office process %s", line)
            except Exception:
                log.warning("could not check/kill Office pid %s", line, exc_info=True)


def _simulated_pdf(src: Path, kind: str, out: Path) -> None:
    """Deterministic stand-in for Office (tests/dev): one page per Word page break /
    per Excel worksheet, each labelled, so pagination flows can be tested."""
    import pymupdf
    pages = 1
    if kind == "docx":
        with zipfile.ZipFile(src) as z:
            xml = z.read("word/document.xml")
        pages = 1 + len(re.findall(rb'<w:br\b[^>]*w:type="page"', xml))
    elif kind == "xlsx":
        with zipfile.ZipFile(src) as z:
            xml = z.read("xl/workbook.xml")
        pages = max(1, len(re.findall(rb"<sheet\b", xml)))
    doc = pymupdf.open()
    for i in range(pages):
        w, h = (842, 595) if kind in ("xlsx", "xls") else (595, 842)   # Excel: landscape pages
        p = doc.new_page(width=w, height=h)
        p.insert_text((60, 90), f"{kind.upper()} page {i + 1} of {pages}", fontsize=28)
        p.draw_rect(p.rect + (30, 30, -30, -30), color=(0.2, 0.3, 0.8), width=2)
    doc.save(str(out))
    doc.close()


class OfficeConverter:
    def __init__(self, cfg):
        self.engine_setting = str(cfg.get("office_converter", "auto")).lower()
        if self.engine_setting not in ENGINES:
            raise ValueError(f"office_converter must be one of {ENGINES}")
        self.libreoffice_cfg = cfg.get("libreoffice_path", "") or ""
        self.timeout = int(cfg.get("office_timeout_seconds", 120))
        self.tmp_root = Path(cfg.data_dir) / "convert_tmp"
        self.tmp_root.mkdir(parents=True, exist_ok=True)
        # Office automation and LibreOffice are not safe to run concurrently: one at a time.
        self._lock = threading.Lock()
        self.sweep()

    # ------------------------------------------------------------------ status
    def engine_for(self, kind: str) -> tuple[str | None, str]:
        """Engine that would convert `kind`, or (None, i18n reason)."""
        e = self.engine_setting
        if e == "none":
            return None, "office.reason.disabled"
        if e == "simulated":
            return "simulated", ""
        app = "word" if kind in WORD_TYPES else "excel"
        if e in ("auto", "msoffice"):
            if _msoffice_installed()[app]:
                return "msoffice", ""
            if e == "msoffice":
                return None, f"office.reason.no_{app}"
        if _find_libreoffice(self.libreoffice_cfg):
            return "libreoffice", ""
        if e == "libreoffice":
            return None, "office.reason.no_libreoffice"
        return None, "office.reason.none_installed"

    def status(self) -> dict:
        ms = _msoffice_installed() if self.engine_setting in ("auto", "msoffice") else {}
        lo = _find_libreoffice(self.libreoffice_cfg)
        word_engine, word_reason = self.engine_for("docx")
        excel_engine, excel_reason = self.engine_for("xlsx")
        return {"setting": self.engine_setting, "word_engine": word_engine, "word_reason": word_reason,
                "excel_engine": excel_engine, "excel_reason": excel_reason,
                "msword_installed": ms.get("word"), "msexcel_installed": ms.get("excel"),
                "libreoffice_path": lo, "timeout_seconds": self.timeout,
                "service_session": windows_session_id() == 0}

    def available_types(self) -> list[str]:
        return [k for k in OFFICE_TYPES if self.engine_for(k)[0]]

    # --------------------------------------------------------------- convert
    def convert(self, src: Path, kind: str, dest_pdf: Path) -> int:
        """Convert `src` (already validated) to `dest_pdf`. Returns milliseconds taken."""
        engine, reason = self.engine_for(kind)
        if engine is None:
            raise OfficeError("err.office_unavailable", reason)
        if not self._lock.acquire(timeout=self.timeout + 60):
            raise OfficeError("err.office_busy")
        self.sweep(min_age_seconds=600)
        work = self.tmp_root / secrets.token_hex(8)
        t0 = time.monotonic()
        try:
            work.mkdir()
            inp = work / f"input.{kind}"            # fixed safe name; client filename never used
            shutil.copyfile(src, inp)
            out = work / "output.pdf"
            if engine == "simulated":
                _simulated_pdf(inp, kind, out)
            elif engine == "msoffice":
                self._run_msoffice(inp, kind, out, work)
            else:
                self._run_libreoffice(inp, out, work)
            if not out.exists() or out.stat().st_size == 0:
                raise OfficeError("err.office_failed", "converter produced no PDF")
            shutil.move(str(out), dest_pdf)
            ms = int((time.monotonic() - t0) * 1000)
            log.info("Converted %s with %s in %d ms", kind, engine, ms)
            return ms
        finally:
            try:
                _remove_dir(work, marker=work.name)
            finally:
                self._lock.release()

    def _run(self, cmd: list[str], cwd: Path, pid_file: Path | None = None, what: str = "converter"):
        """Run a converter process (argument list, no shell) with timeout and cleanup."""
        log.debug("office converter command: %s", cmd)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen(cmd, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, shell=False, creationflags=flags)
        try:
            output, _ = proc.communicate(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc.pid)
            if pid_file is not None:
                _kill_office_pids(pid_file)
            _kill_by_marker(cwd_marker(cmd))
            proc.communicate()
            raise OfficeError("err.office_timeout", f"{what}: no result after {self.timeout} s")
        text = (output or b"").decode("utf-8", "replace").strip()
        if proc.returncode != 0:
            if pid_file is not None:
                _kill_office_pids(pid_file)
            _kill_by_marker(cwd_marker(cmd))
            raise OfficeError("err.office_failed", f"{what} exit {proc.returncode}: {text[-800:]}")
        return text

    def _run_msoffice(self, inp: Path, kind: str, out: Path, work: Path):
        app = "word" if kind in WORD_TYPES else "excel"
        root = Path(__file__).resolve().parent.parent
        pid_file = work / "office.pid"
        cmd = [sys.executable, "-s", "-m", "lps.office_helper", app, str(inp), str(out), str(pid_file)]
        self._run(cmd, root, pid_file=pid_file, what=f"Microsoft {app}")

    def _run_libreoffice(self, inp: Path, out: Path, work: Path):
        soffice = _find_libreoffice(self.libreoffice_cfg)
        # Throw-away user profile in the SHORT system temp folder (C:\Windows\Temp for the
        # service): profiles nest ~120 characters deep, which can exceed MAX_PATH under a
        # long data directory. Its name contains this conversion's marker (work.name).
        profile_dir = Path(tempfile.gettempdir()) / f"lps-lo-{work.name}"
        cmd = [soffice, f"-env:UserInstallation={profile_dir.resolve().as_uri()}", "--headless",
               "--invisible", "--nodefault", "--nofirststartwizard", "--nolockcheck", "--nologo",
               "--norestore", "--convert-to", "pdf", "--outdir", str(work), str(inp)]
        try:
            self._run(cmd, work, what="LibreOffice")
        finally:
            _remove_dir(profile_dir, marker=work.name)
        produced = work / (inp.stem + ".pdf")                   # "input.pdf"
        if produced.exists():
            produced.replace(out)

    def sweep(self, min_age_seconds: float = 0):
        """Remove leftovers of interrupted conversions (at start-up and before conversions)."""
        now = time.time()
        for d in self.tmp_root.iterdir():
            try:
                if d.is_dir() and now - d.stat().st_mtime >= min_age_seconds:
                    shutil.rmtree(_long_path(d), ignore_errors=True)
            except OSError:
                pass


def cwd_marker(cmd: list[str]) -> str:
    """The random conversion-folder name contained in a converter command line."""
    for arg in cmd:
        m = re.search(r"convert_tmp[\\/]([0-9a-f]{16})", arg)
        if m:
            return m.group(1)
    return ""

