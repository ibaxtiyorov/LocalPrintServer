"""Evidence-based printer status (spec §25, final req. §7).

The Epson L1800 reports GetPrinter()['Status'] == 0 whether or not the USB cable
is connected, so a zero status is never shown as "ready". The state is derived
from what can actually be observed:

  * Print Spooler service running?          (service query)
  * printer queue exists / opens?           (OpenPrinter / GetPrinter)
  * non-zero printer status flags           (positive evidence of a problem only)
  * Windows job flags + app job progress    (EnumJobs, stall detection)
  * USB PnP presence (supplementary)        (WMI, Epson vendor id)
  * last completed / failed app job

Every state carries the list of evidence it was derived from.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from . import jobs as J
from .printer.base import JS_PRINTING, JS_PROBLEM, PS_PAUSED, PS_PROBLEM, PrinterError

log = logging.getLogger("lps.status")

READY, IDLE, PRINTING, BUSY, WAITING, PAUSED = "READY", "IDLE", "PRINTING", "BUSY", "WAITING", "PAUSED"
UNAVAILABLE, ERROR, SPOOLER_ERROR = "UNAVAILABLE", "ERROR", "SPOOLER_ERROR"


class StatusService:
    def __init__(self, manager, ttl: float = 3.0):
        self.m = manager
        self.ttl = ttl
        self._lock = threading.Lock()
        self._cache: tuple[float, dict] | None = None

    def get(self, force: bool = False) -> dict:
        with self._lock:
            if not force and self._cache and time.monotonic() - self._cache[0] < self.ttl:
                return self._cache[1]
        st = self._compute()
        with self._lock:
            self._cache = (time.monotonic(), st)
        return st

    def _compute(self) -> dict:
        b, store = self.m.backend, self.m.store
        reasons: list[str] = []
        ev: dict = {"checked_at": datetime.now(timezone.utc).isoformat(), "backend": b.kind}

        spooler = b.spooler_running()
        ev["spooler_running"] = spooler
        snap = b.printer_snapshot() if spooler is not False else {
            "exists": False, "error": "err.spooler_unavailable"}
        ev["printer"] = snap
        spool_jobs = []
        if snap.get("exists"):
            try:
                spool_jobs = b.list_jobs()
            except PrinterError as e:
                ev["enum_jobs_error"] = e.code
        ev["windows_jobs"] = len(spool_jobs)
        ev["usb_present"] = b.usb_present()

        counts = store.counts()
        active = {s: counts.get(s, 0) for s in J.ACTIVE}
        waiting_jobs = store.by_status([J.WAITING])
        last_ok = store.last_with_status(J.COMPLETED)
        ev["last_completed_at"] = last_ok["finished_at"] if last_ok else None
        app_paused = self.m.settings.get("queue_paused")

        if spooler is False or snap.get("error") == "err.spooler_unavailable":
            state = SPOOLER_ERROR
            reasons.append("status.reason.spooler_down")
        elif not snap.get("exists"):
            state = UNAVAILABLE
            reasons.append("status.reason.printer_missing")
        elif snap.get("status", 0) & PS_PAUSED or app_paused:
            state = PAUSED
            reasons.append("status.reason.win_paused" if snap.get("status", 0) & PS_PAUSED
                           else "status.reason.app_paused")
        elif snap.get("status", 0) & PS_PROBLEM or any(j.status & JS_PROBLEM for j in spool_jobs):
            state = ERROR
            reasons.append("status.reason.printer_reports_problem")
        elif waiting_jobs:
            state = WAITING
            reasons.append("status.reason.jobs_waiting")
        elif ev["usb_present"] is False:
            state = UNAVAILABLE
            reasons.append("status.reason.usb_not_detected")
        elif any(j.status & JS_PRINTING for j in spool_jobs) or active.get(J.PRINTING):
            state = PRINTING
        elif spool_jobs or active.get(J.RENDERING) or active.get(J.SUBMITTED) or active.get(J.QUEUED):
            state = BUSY
        elif ev["usb_present"] is True:
            state = READY
            reasons.append("status.reason.ready_evidence")
        else:
            state = IDLE
            reasons.append("status.reason.idle_unverified")
        if snap.get("work_offline"):
            reasons.append("status.reason.work_offline")

        return {
            "state": state,
            "reasons": reasons,
            "evidence": ev,
            "queue": {"app_active": sum(active.values()), "app_queued": active.get(J.QUEUED, 0),
                      "app_waiting": len(waiting_jobs), "windows_jobs": len(spool_jobs),
                      "app_paused": app_paused},
            "worker": {"current_job": self.m.current_job,
                       "threads": self.m.threads_alive()},
        }

    @staticmethod
    def public(st: dict) -> dict:
        """Subset safe for normal users (no internal details)."""
        return {"state": st["state"], "reasons": st["reasons"],
                "queue": {k: st["queue"][k] for k in ("app_active", "app_waiting", "app_paused")},
                "last_completed_at": st["evidence"].get("last_completed_at"),
                "checked_at": st["evidence"]["checked_at"]}
