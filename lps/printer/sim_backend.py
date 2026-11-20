"""Simulated printer for development and automated tests (never used in production).

* Geometry follows the spec's measured shape (≈3 mm margins, borderless = zero
  offsets with ≈1.3 mm expansion) at a reduced DPI to keep PNGs small.
* Each printed sheet is saved as a PNG under data/sim_output/<doc>/ so layout
  can be inspected without paper.
* A fake spooler queue reproduces the verified behaviours: jobs wait with an
  error flag while "USB is disconnected" and print on reconnect (spec §25);
  a spooler restart empties the queue (spec §27).
"""
from __future__ import annotations

import itertools
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

from .. import catalog
from ..layout import Geometry, estimate_geometry
from .base import (JOB_STATUS, JS_ERROR, JS_OFFLINE, JS_PAUSED, JS_PRINTING, JS_SPOOLING,
                   PrinterBackend, PrinterError, PrintSession, SpoolerJob, flag_names, DeviceSettings)

SIM_DPI = 100


class _SimJob:
    def __init__(self, job_id, doc, pages):
        self.job_id = job_id
        self.document = doc
        self.pages = pages
        self.printed = 0
        self.status = JS_SPOOLING
        self.submitted = datetime.now(timezone.utc)
        self.started = None


class SimSession(PrintSession):
    def __init__(self, backend: "SimBackend", s: DeviceSettings):
        self.backend = backend
        self.settings = s
        w_mm, h_mm = s.width_01mm / 10, s.length_01mm / 10
        if s.orientation == catalog.ORIENTATION["LANDSCAPE"]:
            w_mm, h_mm = h_mm, w_mm
        self.geometry = estimate_geometry(w_mm, h_mm, SIM_DPI, s.borderless)
        self.geometry = Geometry(**{**self.geometry.to_dict(), "source": "driver"})
        self.devmode_report = {"merge": None, "driver_adjusted": {}, "simulated": True}
        self._page: Image.Image | None = None
        self._pages: list[Image.Image] = []
        self._doc = None
        self._job_id = None

    def start_doc(self, doc_name):
        self.backend._check_available()
        self._doc = doc_name
        self._job_id = self.backend._reserve_id()
        return self._job_id

    def start_page(self):
        g = self.geometry
        self._page = Image.new("RGB", (g.phys_w, g.phys_h), (255, 255, 255))

    def draw(self, image, dest_device):
        g = self.geometry
        x0, y0, x1, y1 = (int(round(v)) for v in dest_device)
        im = image.resize((max(1, x1 - x0), max(1, y1 - y0)))
        self._page.paste(im, (x0 + g.off_x, y0 + g.off_y))

    def end_page(self):
        self._pages.append(self._page)
        self._page = None
        if self.backend.draw_delay:
            time.sleep(self.backend.draw_delay)

    def end_doc(self):
        self.backend._check_available()
        out = self.backend.output_dir / re.sub(r"[^\w.-]+", "_", self._doc)[:80]
        out.mkdir(parents=True, exist_ok=True)
        for i, p in enumerate(self._pages, 1):
            p.save(out / f"sheet-{i:03d}.png")
        self.backend._add_job(self._job_id, self._doc, len(self._pages) * self.settings.copies)

    def abort(self):
        self._pages = []

    def close(self):
        self._pages = []


class SimBackend(PrinterBackend):
    kind = "simulated"

    def __init__(self, printer_name: str, output_dir: Path, print_seconds: float = 2.0):
        self.printer_name = printer_name
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self._qlock = threading.Lock()
        self._ids = itertools.count(101)
        self._jobs: dict[int, _SimJob] = {}
        self.print_seconds = print_seconds
        self.draw_delay = 0.0
        self.usb_connected = True
        self.spooler_up = True
        self.paused = False
        self.exists = True

    # -- test controls
    def set_usb(self, connected: bool):
        self.usb_connected = connected

    def set_spooler(self, up: bool):
        self.spooler_up = up
        if not up:
            with self._qlock:
                self._jobs.clear()

    def _check_available(self):
        if not self.spooler_up:
            raise PrinterError("err.spooler_unavailable", "simulated spooler down", retryable=True)
        if not self.exists:
            raise PrinterError("err.printer_not_found", "simulated", retryable=True)

    def _reserve_id(self):
        return next(self._ids)

    def _add_job(self, job_id, doc, pages):
        with self._qlock:
            self._jobs[job_id] = _SimJob(job_id, doc, pages)

    def _tick(self):
        """Advance the fake spooler: one job prints at a time, in order."""
        now = time.monotonic()
        with self._qlock:
            for jid in sorted(self._jobs):
                j = self._jobs[jid]
                if self.paused:
                    j.status |= JS_PAUSED
                    break
                j.status &= ~JS_PAUSED
                if not self.usb_connected:
                    j.status = JS_ERROR | JS_OFFLINE
                    j.started = None
                    break
                if j.started is None:
                    j.started = now
                j.status = JS_PRINTING
                frac = (now - j.started) / max(self.print_seconds, 0.001)
                j.printed = min(j.pages, int(frac * j.pages))
                if frac >= 1:
                    del self._jobs[jid]
                    continue
                break

    # -- interface
    def query_geometry(self, s, merge, timeout=None):
        self._check_available()
        return SimSession(self, s).geometry

    def probe(self, s, merge):
        self._check_available()
        sess = SimSession(self, s)
        return sess.geometry, dict(sess.devmode_report)

    def open_session(self, s, merge):
        self._check_available()
        return SimSession(self, s)

    def list_jobs(self):
        self._check_available()
        self._tick()
        with self._qlock:
            return [SpoolerJob(job_id=j.job_id, document=j.document, user="SYSTEM", status=j.status,
                               status_text="", pages_printed=j.printed, total_pages=j.pages,
                               position=pos, submitted=j.submitted.isoformat(),
                               flags=flag_names(j.status, JOB_STATUS))
                    for pos, j in enumerate(sorted(self._jobs.values(), key=lambda x: x.job_id), 1)]

    def cancel_spooler_job(self, job_id):
        self._check_available()
        with self._qlock:
            if self._jobs.pop(int(job_id), None) is None:
                raise PrinterError("err.spooler_job_gone")

    def printer_snapshot(self):
        if not self.spooler_up:
            return {"exists": False, "error": "err.spooler_unavailable", "detail": "simulated"}
        self._tick()
        # Like the real Epson: Status stays 0 even when USB is disconnected (spec §25).
        st = 0x1 if self.paused else 0
        return {"exists": self.exists, "status": st, "status_flags": ["PAUSED"] if self.paused else [],
                "attributes": 0, "work_offline": False, "cjobs": len(self._jobs),
                "port": "USB001", "driver": "EPSON L1800 Series (simulated)"}

    def printer_details(self):
        self._check_available()
        return {"pPrinterName": self.printer_name, "pPortName": "USB001",
                "pDriverName": "EPSON L1800 Series (simulated)", "Status": 0,
                "cJobs": len(self._jobs), "simulated": True,
                "default_devmode": {"PaperSize": 9, "MediaType": 1, "PrintQuality": 360, "Color": 2},
                "driver_info": {"Name": "EPSON L1800 Series", "note": "simulated backend"},
                "capabilities": self.capabilities(), "usb_present": self.usb_present()}

    def capabilities(self, refresh=False):
        ids = [p.dm_paper for p in catalog.PAPER_SIZES] + [catalog.CUSTOM_DM_PAPER]
        names = {p.dm_paper: p.label for p in catalog.PAPER_SIZES}
        # Simulated driver advertises borderless variants for photo sizes only.
        borderless = {"A4": 9001, "P10X15": 9002, "P13X18": 9003}
        for k, pid in borderless.items():
            names[pid] = catalog.PAPER_BY_KEY[k].label + " (Borderless)"
        return {"paper_ids": ids + list(borderless.values()), "paper_names": names,
                "borderless": borderless, "paper_sizes": {}, "errors": [], "simulated": True}

    def invalidate(self):
        pass

    def set_paused(self, paused):
        self._check_available()
        self.paused = paused

    def purge(self):
        self._check_available()
        with self._qlock:
            self._jobs.clear()

    def spooler_running(self):
        return self.spooler_up

    def restart_spooler(self):
        with self.lock:
            self.spooler_up = False
            with self._qlock:
                self._jobs.clear()
            time.sleep(0.2)
            self.spooler_up = True

    def usb_present(self, force=False):
        return self.usb_connected
