"""Windows spooler / GDI backend for the EPSON L1800 (spec §4 verified path).

Per job: printer default DEVMODE copy -> standard public fields -> (optional)
DocumentProperties merge by the driver -> CreateDC('WINSPOOL') ->
CreateDCFromHandle -> StartDoc -> StartPage -> ImageWin.Dib.draw -> EndPage ->
EndDoc -> DeleteDC.

Rules honoured here:
* Only standard DEVMODE fields are written. Epson private DriverData is never
  read-modified-written by this code (spec §15-§21, §36).
* SetPrinter is never called with a DEVMODE, so a job's settings can never
  change the printer defaults or leak into another job (final req. §15).
* Printable geometry always comes from GetDeviceCaps (spec §13).
* GetPrinter()['Status'] == 0 is never treated as "ready" (spec §25).
"""
from __future__ import annotations

import logging
import re
import subprocess
import threading
import time

from PIL import Image, ImageWin

from .. import catalog
from ..layout import Geometry
from .base import (JOB_STATUS, PRINTER_ATTRIBUTE_WORK_OFFLINE, PRINTER_STATUS, DeviceSettings,
                   PrinterBackend, PrinterError, PrintSession, SpoolerJob, flag_names)

import win32con
import win32gui
import win32print
import win32ui

log = logging.getLogger("lps.printer.win32")

HORZRES, VERTRES, LOGPIXELSX, LOGPIXELSY = 8, 10, 88, 90
PHYSICALWIDTH, PHYSICALHEIGHT, PHYSICALOFFSETX, PHYSICALOFFSETY = 110, 111, 112, 113
DM_IN_BUFFER, DM_OUT_BUFFER = 8, 2
DC_PAPERS, DC_PAPERSIZE, DC_PAPERNAMES = 2, 3, 16
DC_BINNAMES, DC_ENUMRESOLUTIONS, DC_COPIES, DC_COLLATE, DC_COLORDEVICE = 12, 13, 18, 22, 32

_SPOOLER_DOWN = {1722, 1726, 1753, 1706, 1717}   # RPC failures when the spooler is stopped
_NOT_FOUND = {1801, 1797, 1905, 1796}
_BORDERLESS_RE = re.compile(r"borderless|без\s+полей|chegarasiz|randlos|sans\s+marges?", re.I)


def _map_error(e: Exception, default: str = "err.print_failed") -> PrinterError:
    if isinstance(e, PrinterError):
        return e
    code = getattr(e, "winerror", None)
    if code is None and getattr(e, "args", None) and isinstance(e.args[0], int):
        code = e.args[0]
    detail = f"{type(e).__name__}: {e}"
    if code in _SPOOLER_DOWN:
        return PrinterError("err.spooler_unavailable", detail, retryable=True)
    if code in _NOT_FOUND:
        return PrinterError("err.printer_not_found", detail, retryable=True)
    if code == 5:
        return PrinterError("err.access_denied", detail)
    return PrinterError(default, detail)


def _norm_paper_name(name: str) -> str:
    name = _BORDERLESS_RE.sub("", name)
    return re.sub(r"[\s()\[\]\-_,.]+", "", name).lower()


def _dm_public(dm) -> dict:
    out = {}
    for k in ("PaperSize", "PaperWidth", "PaperLength", "Orientation", "MediaType", "PrintQuality",
              "YResolution", "Color", "Copies", "Collate", "Duplex", "Fields", "DriverExtra",
              "DeviceName"):
        try:
            out[k] = getattr(dm, k)
        except Exception:
            pass
    return out


class Win32Session(PrintSession):
    def __init__(self, backend: "Win32Backend", dm, doc_settings: DeviceSettings, report: dict):
        self.backend = backend
        self.devmode_report = report
        self._hdc_raw = None
        self._dc = None
        self._doc_started = False
        try:
            self._hdc_raw = win32gui.CreateDC("WINSPOOL", backend.printer_name, dm)
            self._dc = win32ui.CreateDCFromHandle(self._hdc_raw)
            self.geometry = _geometry_from_dc(self._dc.GetDeviceCaps)
        except Exception as e:
            self.close()
            raise _map_error(e, "err.printer_dc_failed")

    @property
    def hdc(self):
        return self._dc.GetSafeHdc()

    def start_doc(self, doc_name: str) -> int | None:
        try:
            # Same GDI StartDoc as dc.StartDoc(), but returns the spooler job id.
            job_id = win32print.StartDoc(self.hdc, (doc_name, None, None, 0))
        except (TypeError, ValueError, AttributeError):
            try:
                self._dc.StartDoc(doc_name)
            except Exception as e:
                raise _map_error(e)
            job_id = None
        except Exception as e:
            raise _map_error(e)
        self._doc_started = True
        return int(job_id) if job_id and job_id > 0 else None

    def start_page(self):
        try:
            self._dc.StartPage()
        except Exception as e:
            raise _map_error(e)

    def draw(self, image: Image.Image, dest_device):
        try:
            ImageWin.Dib(image).draw(self.hdc, tuple(int(round(v)) for v in dest_device))
        except Exception as e:
            raise _map_error(e)

    def end_page(self):
        try:
            self._dc.EndPage()
        except Exception as e:
            raise _map_error(e)

    def end_doc(self):
        try:
            self._dc.EndDoc()
            self._doc_started = False
        except Exception as e:
            raise _map_error(e)

    def abort(self):
        if self._doc_started and self._dc is not None:
            try:
                self._dc.AbortDoc()
            except Exception:
                log.exception("AbortDoc failed")
            self._doc_started = False

    def close(self):
        try:
            if self._dc is not None:
                self._dc.DeleteDC()
            elif self._hdc_raw:
                win32gui.DeleteDC(self._hdc_raw)
        except Exception:
            log.exception("DeleteDC failed")
        self._dc = None
        self._hdc_raw = None


def _geometry_from_dc(getcaps) -> Geometry:
    return Geometry(
        phys_w=getcaps(PHYSICALWIDTH), phys_h=getcaps(PHYSICALHEIGHT),
        off_x=getcaps(PHYSICALOFFSETX), off_y=getcaps(PHYSICALOFFSETY),
        horz=getcaps(HORZRES), vert=getcaps(VERTRES),
        dpi_x=getcaps(LOGPIXELSX), dpi_y=getcaps(LOGPIXELSY), source="driver")


class Win32Backend(PrinterBackend):
    kind = "win32"

    def __init__(self, printer_name: str, usb_probe_enabled: bool = True,
                 usb_vendor_id: str = "04B8"):
        self.printer_name = printer_name
        # Serialises every DC/DEVMODE operation on this printer (spec §28).
        self.lock = threading.RLock()
        self._usb_enabled = usb_probe_enabled
        self._usb_vendor = re.sub(r"[^0-9A-Fa-f]", "", usb_vendor_id)[:4].upper() or "04B8"
        self._usb_cache: tuple[float, bool | None] = (0.0, None)
        self._caps_cache: dict | None = None

    # ------------------------------------------------------------------ helpers
    def _open(self, access: int | None = None):
        try:
            if access is None:
                return win32print.OpenPrinter(self.printer_name)
            return win32print.OpenPrinter(self.printer_name, {"DesiredAccess": access})
        except Exception as e:
            if self.spooler_running() is False:
                raise PrinterError("err.spooler_unavailable", str(e), retryable=True)
            raise _map_error(e, "err.printer_not_found")

    def _build_devmode(self, s: DeviceSettings, merge: bool):
        h = self._open()
        try:
            info = win32print.GetPrinter(h, 2)
            dm = info.get("pDevMode")
            if dm is None:
                raise PrinterError("err.printer_dc_failed", "printer has no default DEVMODE")
            before = _dm_public(dm)
            fields = dm.Fields
            dm.Orientation = s.orientation
            dm.PaperSize = s.paper
            dm.PaperWidth = s.width_01mm
            dm.PaperLength = s.length_01mm
            fields |= catalog.DM_ORIENTATION | catalog.DM_PAPERSIZE
            if s.custom:
                fields |= catalog.DM_PAPERWIDTH | catalog.DM_PAPERLENGTH
            else:
                fields &= ~(catalog.DM_PAPERWIDTH | catalog.DM_PAPERLENGTH)
            dm.MediaType = s.media
            dm.PrintQuality = s.print_quality
            dm.YResolution = s.y_resolution
            dm.Color = s.color
            dm.Duplex = catalog.DUPLEX_SIMPLEX
            dm.Copies = s.copies
            dm.Collate = 1 if s.collate else 0
            fields |= (catalog.DM_MEDIATYPE | catalog.DM_PRINTQUALITY | catalog.DM_YRESOLUTION
                       | catalog.DM_COLOR | catalog.DM_DUPLEX | catalog.DM_COPIES
                       | catalog.DM_COLLATE)
            dm.Fields = fields
            requested = _dm_public(dm)
            merged_ok = None
            if merge:
                # Documented way to let the driver reconcile its private data with
                # the public fields. The driver, not this code, updates DriverData.
                try:
                    rc = win32print.DocumentProperties(0, h, self.printer_name, dm, dm,
                                                       DM_IN_BUFFER | DM_OUT_BUFFER)
                    merged_ok = rc == win32con.IDOK if hasattr(win32con, "IDOK") else rc == 1
                except Exception:
                    log.exception("DocumentProperties merge failed; using unmerged DEVMODE")
                    merged_ok = False
            after = _dm_public(dm)
            adjusted = {k: (requested[k], after[k]) for k in
                        ("PaperSize", "PaperWidth", "PaperLength", "Orientation", "MediaType",
                         "PrintQuality", "YResolution", "Color", "Copies", "Collate")
                        if k in requested and requested.get(k) != after.get(k)}
            report = {"merge": merge, "merge_ok": merged_ok, "driver_adjusted": adjusted,
                      "printer_default": {k: before.get(k) for k in ("PaperSize", "MediaType",
                                                                      "PrintQuality", "Color")}}
            if adjusted:
                log.warning("Driver adjusted DEVMODE fields: %s", adjusted)
            return dm, report
        finally:
            win32print.ClosePrinter(h)

    # ---------------------------------------------------------------- geometry
    def query_geometry(self, s: DeviceSettings, merge: bool, timeout: float | None = None) -> Geometry:
        if not self.lock.acquire(timeout=-1 if timeout is None else timeout):
            raise PrinterError("err.printer_busy", retryable=True)
        try:
            dm, _ = self._build_devmode(s, merge)
            hdc = None
            try:
                # A DC without StartDoc creates no spooler job and uses no paper.
                hdc = win32gui.CreateDC("WINSPOOL", self.printer_name, dm)
                return _geometry_from_dc(lambda i: win32print.GetDeviceCaps(hdc, i))
            except Exception as e:
                raise _map_error(e, "err.printer_dc_failed")
            finally:
                if hdc:
                    win32gui.DeleteDC(hdc)
        finally:
            self.lock.release()

    def probe(self, s: DeviceSettings, merge: bool) -> tuple[Geometry, dict]:
        """DEVMODE build + driver validation + DC geometry. No StartDoc: no job, no paper."""
        with self.lock:
            dm, report = self._build_devmode(s, merge)
            hdc = None
            try:
                hdc = win32gui.CreateDC("WINSPOOL", self.printer_name, dm)
                g = _geometry_from_dc(lambda i: win32print.GetDeviceCaps(hdc, i))
                report["dc_devmode"] = {k: v for k, v in _dm_public(dm).items()
                                        if k in ("PaperSize", "MediaType", "PrintQuality",
                                                 "YResolution", "Orientation")}
                return g, report
            except PrinterError:
                raise
            except Exception as e:
                raise _map_error(e, "err.printer_dc_failed")
            finally:
                if hdc:
                    win32gui.DeleteDC(hdc)

    def open_session(self, s: DeviceSettings, merge: bool) -> Win32Session:
        dm, report = self._build_devmode(s, merge)
        return Win32Session(self, dm, s, report)

    # ------------------------------------------------------------------ jobs
    def list_jobs(self) -> list[SpoolerJob]:
        h = self._open()
        try:
            raw = win32print.EnumJobs(h, 0, 999, 1)
        except Exception as e:
            raise _map_error(e, "err.spooler_unavailable")
        finally:
            win32print.ClosePrinter(h)
        out = []
        for j in raw:
            st = int(j.get("Status") or 0)
            sub = j.get("Submitted")
            out.append(SpoolerJob(
                job_id=int(j["JobId"]), document=j.get("pDocument") or "",
                user=j.get("pUserName") or "", status=st, status_text=j.get("pStatus") or "",
                pages_printed=int(j.get("PagesPrinted") or 0),
                total_pages=int(j.get("TotalPages") or 0), position=int(j.get("Position") or 0),
                submitted=sub.isoformat() if hasattr(sub, "isoformat") else (str(sub) if sub else None),
                machine=j.get("pMachineName") or "", flags=flag_names(st, JOB_STATUS)))
        return out

    def cancel_spooler_job(self, job_id: int):
        for access in (win32print.PRINTER_ALL_ACCESS, win32print.PRINTER_ACCESS_USE):
            try:
                h = win32print.OpenPrinter(self.printer_name, {"DesiredAccess": access})
            except Exception as e:
                last = e
                continue
            try:
                win32print.SetJob(h, int(job_id), 0, None, win32print.JOB_CONTROL_DELETE)
                return
            except Exception as e:
                last = e
            finally:
                win32print.ClosePrinter(h)
        code = getattr(last, "winerror", None)
        if code == 87:   # ERROR_INVALID_PARAMETER: job no longer exists
            raise PrinterError("err.spooler_job_gone", str(last))
        raise _map_error(last, "err.cancel_failed")

    def _control(self, command: int):
        try:
            h = win32print.OpenPrinter(self.printer_name,
                                       {"DesiredAccess": win32print.PRINTER_ALL_ACCESS})
        except Exception as e:
            raise _map_error(e, "err.access_denied")
        try:
            win32print.SetPrinter(h, 0, None, command)
        except Exception as e:
            raise _map_error(e, "err.access_denied")
        finally:
            win32print.ClosePrinter(h)

    def set_paused(self, paused: bool):
        self._control(win32print.PRINTER_CONTROL_PAUSE if paused
                      else win32print.PRINTER_CONTROL_RESUME)

    def purge(self):
        self._control(win32print.PRINTER_CONTROL_PURGE)

    # ---------------------------------------------------------------- status
    def printer_snapshot(self) -> dict:
        try:
            h = self._open()
        except PrinterError as e:
            return {"exists": False, "error": e.code, "detail": e.detail}
        try:
            info = win32print.GetPrinter(h, 2)
        except Exception as e:
            err = _map_error(e, "err.spooler_unavailable")
            return {"exists": False, "error": err.code, "detail": err.detail}
        finally:
            win32print.ClosePrinter(h)
        st, attrs = int(info.get("Status") or 0), int(info.get("Attributes") or 0)
        return {"exists": True, "status": st, "status_flags": flag_names(st, PRINTER_STATUS),
                "attributes": attrs, "work_offline": bool(attrs & PRINTER_ATTRIBUTE_WORK_OFFLINE),
                "cjobs": int(info.get("cJobs") or 0), "port": info.get("pPortName"),
                "driver": info.get("pDriverName")}

    def printer_details(self) -> dict:
        h = self._open()
        try:
            info = win32print.GetPrinter(h, 2)
        finally:
            win32print.ClosePrinter(h)
        dm = info.get("pDevMode")
        details = {k: v for k, v in info.items()
                   if k not in ("pDevMode", "pSecurityDescriptor") and not isinstance(v, bytes)}
        details["default_devmode"] = _dm_public(dm) if dm is not None else None
        details["driver_info"] = self._driver_info(info.get("pDriverName"))
        details["capabilities"] = self.capabilities(refresh=True)
        details["usb_present"] = self.usb_present(force=True)
        return details

    def _driver_info(self, name: str | None) -> dict | None:
        for level in (6, 3, 2):
            try:
                for d in win32print.EnumPrinterDrivers(None, None, level):
                    if d.get("Name") == name:
                        return {k: (str(v) if not isinstance(v, (int, str, list, type(None))) else v)
                                for k, v in d.items()}
            except Exception:
                continue
        return None

    def capabilities(self, refresh: bool = False) -> dict:
        if self._caps_cache is not None and not refresh:
            return self._caps_cache
        caps: dict = {"paper_ids": [], "paper_names": {}, "paper_sizes": {}, "borderless": {},
                      "errors": []}
        try:
            h = self._open()
            try:
                port = win32print.GetPrinter(h, 2).get("pPortName") or ""
            finally:
                win32print.ClosePrinter(h)
        except PrinterError as e:
            caps["errors"].append(e.code)
            return caps   # not cached: retry next time

        def dc(cap):
            try:
                return win32print.DeviceCapabilities(self.printer_name, port, cap)
            except Exception as e:
                caps["errors"].append(f"cap {cap}: {e}")
                return None

        ids = list(dc(DC_PAPERS) or [])
        names = list(dc(DC_PAPERNAMES) or [])
        sizes = list(dc(DC_PAPERSIZE) or [])
        caps["paper_ids"] = ids
        caps["paper_names"] = {int(i): str(n).strip("\x00 ") for i, n in zip(ids, names)}
        for i, sz in zip(ids, sizes):
            try:
                caps["paper_sizes"][int(i)] = (int(sz["x"]), int(sz["y"]))
            except Exception:
                try:
                    caps["paper_sizes"][int(i)] = (int(sz[0]), int(sz[1]))
                except Exception:
                    pass
        caps["resolutions"] = [list(r) if isinstance(r, (tuple, list)) else r
                               for r in (dc(DC_ENUMRESOLUTIONS) or [])]
        caps["max_copies"] = dc(DC_COPIES)
        caps["collate"] = dc(DC_COLLATE)
        caps["color_device"] = dc(DC_COLORDEVICE)
        caps["bins"] = list(dc(DC_BINNAMES) or [])
        # Borderless: only when the driver itself advertises a separate
        # "... (Borderless)" paper whose base name matches a verified size.
        by_norm = {}
        for p in catalog.PAPER_SIZES:
            nm = caps["paper_names"].get(p.dm_paper)
            if nm:
                by_norm[_norm_paper_name(nm)] = p.key
        for pid, nm in caps["paper_names"].items():
            if _BORDERLESS_RE.search(nm):
                key = by_norm.get(_norm_paper_name(nm))
                if key and key not in caps["borderless"]:
                    caps["borderless"][key] = pid
        self._caps_cache = caps
        return caps

    def invalidate(self):
        self._caps_cache = None

    # --------------------------------------------------------------- spooler
    def spooler_running(self) -> bool | None:
        try:
            import win32service
            import win32serviceutil
            return win32serviceutil.QueryServiceStatus("Spooler")[1] == win32service.SERVICE_RUNNING
        except Exception:
            log.debug("Spooler service query failed", exc_info=True)
            return None

    def restart_spooler(self):
        with self.lock:
            cmd = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                   "Restart-Service -Name Spooler -Force -ErrorAction Stop"]
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except Exception as e:
                raise PrinterError("err.spooler_restart_failed", str(e))
            if r.returncode != 0:
                msg = (r.stderr or r.stdout or "").strip()[:500]
                if "denied" in msg.lower() or "cannot open" in msg.lower():
                    raise PrinterError("err.access_denied", msg)
                raise PrinterError("err.spooler_restart_failed", msg)
            for _ in range(30):
                if self.spooler_running():
                    break
                time.sleep(1)
            else:
                raise PrinterError("err.spooler_restart_failed", "spooler did not come back")
            self.invalidate()

    def usb_present(self, force: bool = False) -> bool | None:
        """Supplementary evidence only: is an Epson USB device enumerated by PnP?"""
        if not self._usb_enabled:
            return None
        ts, val = self._usb_cache
        if not force and time.monotonic() - ts < 10:
            return val
        val = None
        try:
            import pythoncom
            import win32com.client
            pythoncom.CoInitialize()
            try:
                wmi = win32com.client.GetObject(r"winmgmts:\\.\root\cimv2")
                q = ("SELECT DeviceID FROM Win32_PnPEntity WHERE DeviceID LIKE "
                     f"'USB\\\\VID_{self._usb_vendor}%'")
                val = len(list(wmi.ExecQuery(q))) > 0
            finally:
                pythoncom.CoUninitialize()
        except Exception:
            log.debug("USB probe failed", exc_info=True)
            val = None
        self._usb_cache = (time.monotonic(), val)
        return val

    def launch_properties(self, preferences: bool):
        """Opens the Windows dialog on the *server's* interactive desktop (only
        possible when the app is not running as a session-0 service)."""
        flag = "/e" if preferences else "/p"
        try:
            subprocess.Popen(["rundll32.exe", "printui.dll,PrintUIEntry", flag, "/n",
                              self.printer_name])
        except Exception as e:
            raise PrinterError("err.not_supported", str(e))
