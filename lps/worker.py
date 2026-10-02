"""Print manager: single print worker, spooler monitor, cleanup.

Concurrency model (spec §28, final req. §15):
* Web threads only create DB records and wake the worker.
* Exactly one worker thread opens printer DCs and submits jobs, holding the
  backend lock for the whole DC lifetime. Each job gets its own DEVMODE copy
  built from its own stored options, so settings cannot leak between jobs.
* A monitor thread follows submitted jobs through EnumJobs and derives
  PRINTING / WAITING / COMPLETED / UNKNOWN from observed spooler state.
"""
from __future__ import annotations

import logging
import shutil
import threading
import time
from datetime import datetime, timedelta, timezone

from . import jobs as J
from .audit import AuditLog
from .config import Config
from .db import parse_ts
from .documents import DocumentError, inspect_upload, open_parts, render_type
from .layout import Geometry, LayoutError, estimate_geometry, output_sequence
from .options import PrintOptions
from .printer.base import (JS_BLOCKED, JS_COMPLETE, JS_DELETED, JS_DELETING, JS_ERROR, JS_OFFLINE,
                           JS_PAPEROUT, JS_PAUSED, JS_PRINTED, JS_PRINTING, JS_PROBLEM,
                           JS_USER_INTERVENTION, PrinterBackend, PrinterError,
                           build_device_settings)
from .render import placement_image, plan_sheet, prepare
from .settings_service import Settings

log = logging.getLogger("lps.worker")


class CancelledByUser(Exception):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _problem_detail(flags: int) -> str:
    parts = []
    if flags & JS_OFFLINE:
        parts.append("printer offline")
    if flags & JS_PAPEROUT:
        parts.append("paper out")
    if flags & JS_USER_INTERVENTION:
        parts.append("printer needs attention")
    if flags & JS_BLOCKED:
        parts.append("blocked")
    if flags & JS_ERROR:
        parts.append("printer error / not ready")
    return ", ".join(parts) or "problem reported"


class PrintManager:
    def __init__(self, cfg: Config, settings: Settings, store: J.JobStore, backend: PrinterBackend,
                 audit: AuditLog):
        self.cfg = cfg
        self.settings = settings
        self.store = store
        self.backend = backend
        self.audit = audit
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._geo_cache: dict[tuple, Geometry] = {}
        self._geo_lock = threading.Lock()
        # Serialises "job reaches the Windows queue" with cancel requests so a cancel that
        # arrives during rendering is always applied exactly once (never lost, never doubled).
        self._cancel_lock = threading.RLock()
        self.last_spooler_restart: datetime | None = None
        self.spooler_down_since: datetime | None = None
        # Last moment the spooler was observed failing. A restart happened after this, so
        # only jobs submitted before it can have been lost by the outage.
        self.spooler_last_fail_at: datetime | None = None
        self.worker_heartbeat: datetime | None = None
        self.monitor_heartbeat: datetime | None = None
        self.current_job: str | None = None
        self.last_spooler_jobs: list = []
        settings.on_change(lambda changed: self._on_settings(changed))

    # ------------------------------------------------------------ lifecycle
    def start(self):
        recovered = self.store.recover_after_restart()
        if recovered:
            log.warning("Marked %d interrupted job(s) as FAILED after restart: %s",
                        len(recovered), recovered)
        self._cleanup_orphans()
        for name, target in (("print-worker", self._worker_loop),
                             ("spooler-monitor", self._monitor_loop),
                             ("cleanup", self._cleanup_loop)):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self):
        self._stop.set()
        self._wake.set()
        for t in self._threads:
            t.join(timeout=10)

    def wake(self):
        self._wake.set()

    def threads_alive(self) -> dict[str, bool]:
        return {t.name: t.is_alive() for t in self._threads}

    def _on_settings(self, changed: dict):
        if {"devmode_merge", "borderless_enabled"} & set(changed):
            self.invalidate_geometry()
        if "queue_paused" in changed:
            self.wake()

    # ------------------------------------------------------------- geometry
    def invalidate_geometry(self):
        with self._geo_lock:
            self._geo_cache.clear()
        if hasattr(self.backend, "invalidate"):
            self.backend.invalidate()

    def capabilities(self) -> dict:
        try:
            return self.backend.capabilities()
        except Exception:
            log.exception("capabilities query failed")
            return {"paper_ids": [], "borderless": {}, "errors": ["query failed"]}

    def available_papers(self, caps: dict | None = None) -> set[str]:
        from . import catalog
        caps = caps if caps is not None else self.capabilities()
        ids = set(caps.get("paper_ids") or [])
        keys = {p.key for p in catalog.PAPER_SIZES}
        if not ids:
            return keys          # driver not reachable: keep verified list
        missing = {p.key for p in catalog.PAPER_SIZES if p.dm_paper not in ids}
        if missing:
            log.debug("Verified paper ids not advertised by driver: %s", missing)
        return keys - missing

    def borderless_papers(self, caps: dict | None = None) -> dict[str, int]:
        if not self.settings.get("borderless_enabled"):
            return {}
        caps = caps if caps is not None else self.capabilities()
        return dict(caps.get("borderless") or {})

    def preview_geometry(self, opts: PrintOptions, orientation: str) -> Geometry:
        from .render import oriented_sheet_mm
        s = self.settings.all()
        ds = build_device_settings(opts, orientation, driver_copies=True,
                                   borderless_papers=self.borderless_papers())
        key = ds.cache_key() + (s["devmode_merge"],)
        with self._geo_lock:
            g = self._geo_cache.get(key)
        if g:
            return g
        try:
            g = self.backend.query_geometry(ds, s["devmode_merge"], timeout=2.0)
            with self._geo_lock:
                self._geo_cache[key] = g
            return g
        except PrinterError as e:
            log.debug("Preview geometry falls back to estimate: %s", e)
        except Exception:
            log.exception("Preview geometry query failed")
        w, h = oriented_sheet_mm(opts, orientation)
        return estimate_geometry(w, h, 150, opts.borderless)

    # --------------------------------------------------------------- submit
    def enqueue(self, job_id: str):
        """RECEIVED -> VALIDATING -> QUEUED (re-verifies the stored file)."""
        job = self.store.get(job_id)
        self.store.transition(job_id, J.VALIDATING, expect=J.RECEIVED)
        try:
            s = self.settings.all()
            for part in self.store.parts(job):
                if not part.get("stored_name"):
                    raise DocumentError("err.file_missing")
                path = self.cfg.jobs_dir / part["stored_name"]
                want = render_type(part["doc_type"])       # Word/Excel are stored as PDF
                allowed = list(s["allowed_types"]) + ([want] if want != part["doc_type"] else [])
                doc_type, pages = inspect_upload(path, allowed, s["max_pdf_pages"])
                if doc_type != want or pages != part["page_count"]:
                    raise DocumentError("err.corrupt_file", "file changed after upload")
        except DocumentError as e:
            self.store.transition(job_id, J.FAILED, expect=J.VALIDATING, error_code=e.code,
                                  error_message=e.detail[:500])
            self._delete_job_file(job)
            return False
        self.store.transition(job_id, J.QUEUED, expect=J.VALIDATING)
        self.wake()
        return True

    # --------------------------------------------------------------- cancel
    def cancel(self, job_id: str, actor: str) -> str:
        """Returns 'cancelled', 'cancelling', 'final' or raises PrinterError."""
        with self._cancel_lock:
            return self._cancel_locked(job_id, actor)

    def _cancel_locked(self, job_id: str, actor: str) -> str:
        job = self.store.get(job_id)
        if job is None or job["status"] in J.FINAL:
            return "final"
        if self.store.transition(job_id, J.CANCELLED, expect=list(J.PRE_SPOOL),
                                 detail=f"cancelled by {actor} before printing"):
            self._delete_job_file(job)
            return "cancelled"
        self.store.update(job_id, cancel_requested=1)
        job = self.store.get(job_id)
        if job["status"] == J.RENDERING:
            return "cancelling"         # worker aborts the document at the next page
        if job["status"] in J.IN_SPOOLER and job["spooler_job_id"]:
            try:
                self.backend.cancel_spooler_job(job["spooler_job_id"])
            except PrinterError as e:
                if e.code == "err.spooler_job_gone":
                    self.store.update(job_id, cancel_requested=0)
                    return "final"
                self.store.update(job_id, cancel_requested=0)
                raise
            return "cancelling"
        if job["status"] in J.IN_SPOOLER:
            return "cancelling"
        return "final"

    def cancel_all(self, actor: str) -> dict:
        res = {"cancelled": 0, "cancelling": 0, "failed": 0}
        for job in self.store.by_status(J.ACTIVE):
            try:
                r = self.cancel(job["id"], actor)
                if r in res:
                    res[r] += 1
            except PrinterError:
                res["failed"] += 1
        return res

    # --------------------------------------------------------------- worker
    def _worker_loop(self):
        log.info("Print worker started (backend=%s, printer=%s)", self.backend.kind,
                 self.backend.printer_name)
        while not self._stop.is_set():
            self.worker_heartbeat = _now()
            try:
                if self.settings.get("queue_paused"):
                    self._wait(5)
                    continue
                inflight = len(self.store.by_status(J.IN_SPOOLER))
                if inflight >= self.settings.get("max_inflight_spooler_jobs"):
                    self._wait(2)
                    continue
                job = self.store.claim_next()
                if job is None:
                    self._wait(5)
                    continue
                self.current_job = job["id"]
                try:
                    self._process(job)
                finally:
                    self.current_job = None
            except Exception:
                log.exception("Unexpected error in print worker loop")
                self._wait(5)

    def _wait(self, seconds: float):
        self._wake.wait(seconds)
        self._wake.clear()

    def _check_cancel(self, job_id: str):
        j = self.store.get(job_id)
        if j and j["cancel_requested"]:
            raise CancelledByUser()

    def _process(self, job: dict):
        jid = job["id"]
        s = self.settings.all()
        try:
            opts = self.store.options(job)
            # All documents of the job as one page sequence; opts.pages holds only the
            # selected pages, so unselected pages are never rendered or sent.
            with open_parts(self.store.parts(job), self.cfg.jobs_dir,
                            pdf_render_dpi=s["pdf_render_dpi"],
                            default_image_dpi=s["default_image_dpi"]) as doc:
                prep = prepare(doc, opts)
                driver_copies = s["copies_mode"] == "DRIVER"
                borderless = self.borderless_papers() if opts.borderless else {}
                if opts.borderless and opts.paper not in borderless:
                    raise PrinterError("err.borderless_unavailable")
                ds = build_device_settings(opts, prep.orientation, driver_copies=driver_copies,
                                           borderless_papers=borderless)
                n_sheets = len(prep.plan.sheets)
                sequence = (list(range(n_sheets)) if driver_copies
                            else output_sequence(n_sheets, opts.copies, opts.collate))
                self.store.update(jid, sheet_count=n_sheets, attempts=job["attempts"] + 1)
                doc_name = f"LPS-{jid} {job['filename']}"[:120]
                log.info("job %s: rendering %s, %d sheet(s), sequence=%d, options: %s, device: %s",
                         jid, job["filename"], n_sheets, len(sequence), opts.summary(),
                         ds.to_dict())
                spooler_id = self._submit(jid, doc, prep, ds, sequence, doc_name, s)
            if spooler_id is None:
                spooler_id = self._find_spooler_job(doc_name)
            with self._cancel_lock:
                self.store.transition(jid, J.SUBMITTED, expect=J.RENDERING,
                                      detail="in Windows print queue", spooler_job_id=spooler_id,
                                      spooler_doc_name=doc_name, error_code=None)
                log.info("job %s: submitted to spooler (Windows job id %s)", jid, spooler_id)
                if self.store.get(jid)["cancel_requested"]:
                    # Cancel arrived after the last between-pages check: remove the job from
                    # the Windows queue now so it is not printed.
                    self._delete_submitted(jid, spooler_id)
        except CancelledByUser:
            self.store.transition(jid, J.CANCELLED, detail="cancelled while rendering")
            self._delete_job_file(job)
        except DocumentError as e:
            log.warning("job %s: document error %s", jid, e)
            self.store.transition(jid, J.FAILED, error_code=e.code, error_message=e.detail[:500])
            self._delete_job_file(job)
        except PrinterError as e:
            self._printer_failure(job, e, s)
        except LayoutError as e:
            log.warning("job %s: layout error %s", jid, e)
            self.store.transition(jid, J.FAILED, error_code="err.margins", error_message=str(e))
            self._delete_job_file(job)
        except MemoryError:
            log.exception("job %s: out of memory", jid)
            self.store.transition(jid, J.FAILED, error_code="err.render_failed",
                                  error_message="out of memory")
            self._delete_job_file(job)
        except Exception as e:
            log.exception("job %s: unexpected failure", jid)
            self.store.transition(jid, J.FAILED, error_code="err.internal",
                                  error_message=f"{type(e).__name__}: {e}"[:500])
            self._delete_job_file(job)

    def _submit(self, jid, doc, prep, ds, sequence, doc_name, s) -> int | None:
        with self.backend.lock:
            session = self.backend.open_session(ds, s["devmode_merge"])
            try:
                g = session.geometry
                log.info("job %s: DC geometry %s; devmode %s", jid, g.to_dict(),
                         session.devmode_report)
                self._check_paper(jid, prep, g)
                spooler_id = session.start_doc(doc_name)
                try:
                    mono = prep.options.color == "MONO"
                    for n, sheet_idx in enumerate(sequence, 1):
                        self._check_cancel(jid)
                        sp = plan_sheet(prep, g, sheet_idx, s["nup_gap_mm"])
                        session.start_page()
                        for p in sp.placements:
                            d = p.dest
                            dw, dh = d[2] - d[0], d[3] - d[1]
                            img = placement_image(doc, p, dw, dh, mono)
                            session.draw(img, (d[0] - g.off_x, d[1] - g.off_y,
                                               d[2] - g.off_x, d[3] - g.off_y))
                            del img
                        session.end_page()
                        self.store.update(jid, status_detail=f"rendered sheet {n}/{len(sequence)}")
                    self._check_cancel(jid)
                    session.end_doc()
                except BaseException:
                    session.abort()
                    raise
                return spooler_id
            finally:
                session.close()

    def _delete_submitted(self, jid: str, spooler_id: int | None):
        if not spooler_id:
            log.warning("job %s: cancel requested but Windows job id unknown", jid)
            return
        try:
            self.backend.cancel_spooler_job(spooler_id)
            log.info("job %s: late cancel - deleted Windows job %s", jid, spooler_id)
        except PrinterError as e:
            log.warning("job %s: late cancel of Windows job %s failed: %s", jid, spooler_id, e)

    def _check_paper(self, jid, prep, g: Geometry):
        from .render import oriented_sheet_mm
        w, h = oriented_sheet_mm(prep.options, prep.orientation)
        gw, gh = g.phys_w / g.dpi_x * 25.4, g.phys_h / g.dpi_y * 25.4
        if abs(gw - w) > 0.15 * w or abs(gh - h) > 0.15 * h:
            # Warning only: verified custom output (spec §7, §13) shows DC sizes can differ
            # from nominal; layout always uses the DC geometry itself.
            log.warning("job %s: DC physical size %.1fx%.1f mm differs from requested %.1fx%.1f mm",
                        jid, gw, gh, w, h)

    def _find_spooler_job(self, doc_name: str) -> int | None:
        try:
            for sj in self.backend.list_jobs():
                if sj.document == doc_name:
                    return sj.job_id
        except PrinterError:
            pass
        return None

    def _printer_failure(self, job, e: PrinterError, s):
        jid = job["id"]
        if self.store.get(jid)["cancel_requested"]:
            self.store.transition(jid, J.CANCELLED, detail="cancelled while rendering")
            self._delete_job_file(job)
            return
        queued = parse_ts(job["queued_at"]) or _now()
        deadline = queued + timedelta(minutes=s["spooler_retry_minutes"])
        if e.retryable and _now() < deadline:
            delay = min(60, 5 * 2 ** min(job["attempts"], 4))
            nxt = (_now() + timedelta(seconds=delay)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            log.warning("job %s: transient printer error %s; retry in %ss", jid, e, delay)
            self.store.transition(jid, J.QUEUED, expect=J.RENDERING, error_code=e.code,
                                  detail=f"waiting for printer/spooler: {e.code}",
                                  next_attempt_at=nxt, queued_at=job["queued_at"])
            if e.code == "err.spooler_unavailable":
                self.spooler_last_fail_at = _now()
                if self.spooler_down_since is None:
                    self.spooler_down_since = _now()
            self.invalidate_geometry()
            return
        log.error("job %s: printer error %s", jid, e)
        self.store.transition(jid, J.FAILED, error_code=e.code, error_message=e.detail[:500])
        self._delete_job_file(job)

    # -------------------------------------------------------------- monitor
    def _monitor_loop(self):
        while not self._stop.is_set():
            self.monitor_heartbeat = _now()
            try:
                self._monitor_once()
            except Exception:
                log.exception("Spooler monitor error")
            self._stop.wait(2)

    def _monitor_once(self):
        tracked = self.store.by_status(J.IN_SPOOLER)
        try:
            spool = self.backend.list_jobs()
            if self.spooler_down_since is not None:
                log.warning("Spooler reachable again (was down since %s)", self.spooler_down_since)
                self.last_spooler_restart = self.spooler_last_fail_at or self.spooler_down_since
                self.spooler_down_since = None
                self.invalidate_geometry()
                self.wake()
        except PrinterError as e:
            self.spooler_last_fail_at = _now()
            if self.spooler_down_since is None:
                self.spooler_down_since = _now()
                log.error("Spooler/printer unreachable: %s", e)
            for j in tracked:
                if j["status"] != J.WAITING or j["status_detail"] != "spooler unavailable":
                    self.store.transition(j["id"], J.WAITING, detail="spooler unavailable")
            return
        self.last_spooler_jobs = spool
        if not tracked:
            return
        by_id = {sj.job_id: sj for sj in spool}
        by_doc = {sj.document: sj for sj in spool}
        stall = self.settings.get("stall_seconds")
        for j in tracked:
            sj = by_id.get(j["spooler_job_id"]) if j["spooler_job_id"] else None
            if sj is None and j["spooler_doc_name"]:
                sj = by_doc.get(j["spooler_doc_name"])
                if sj and not j["spooler_job_id"]:
                    self.store.update(j["id"], spooler_job_id=sj.job_id)
            if sj is None:
                self._job_left_queue(j)
                continue
            st = sj.status
            progress = (f"page {sj.pages_printed}/{sj.total_pages}" if sj.total_pages else "")
            if st & (JS_PRINTED | JS_COMPLETE) and not st & JS_PRINTING:
                self.store.transition(j["id"], J.COMPLETED, detail="spooler reports printed")
                self._delete_job_file(j)
            elif st & (JS_DELETING | JS_DELETED):
                self._set_detail(j, j["status"], "being deleted from Windows queue")
            elif st & JS_PROBLEM:
                self._set_detail(j, J.WAITING, f"printer not ready: {_problem_detail(st)}")
            elif st & JS_PAUSED:
                self._set_detail(j, J.WAITING, "job paused in Windows queue")
            elif st & JS_PRINTING:
                if not j["spooler_seen_printing"]:
                    self.store.update(j["id"], spooler_seen_printing=1)
                self._set_detail(j, J.PRINTING, progress or "printing")
            else:
                since = parse_ts(j["submitted_at"]) or _now()
                if sj.position > 1:
                    self._set_detail(j, j["status"],
                                     f"waiting behind {sj.position - 1} other Windows job(s)")
                elif (_now() - since).total_seconds() > stall and not sj.pages_printed:
                    self._set_detail(j, J.WAITING,
                                     "printer not responding - check USB cable, power and paper")
                elif j["status"] == J.SUBMITTED:
                    self._set_detail(j, J.SUBMITTED, f"in Windows queue (position {sj.position})")

    def _set_detail(self, j, status, detail):
        if j["status"] != status or j["status_detail"] != detail:
            self.store.transition(j["id"], status, detail=detail)

    def _job_left_queue(self, j):
        if j["cancel_requested"]:
            self.store.transition(j["id"], J.CANCELLED,
                                  detail="removed from Windows queue; pages already sent to the"
                                         " printer may still print")
        elif (self.last_spooler_restart and parse_ts(j["submitted_at"])
              and self.last_spooler_restart > parse_ts(j["submitted_at"])):
            self.store.transition(j["id"], J.UNKNOWN,
                                  detail="job vanished after a Print Spooler restart; outcome unknown")
        elif j["spooler_seen_printing"] or j["status"] in (J.SUBMITTED, J.PRINTING):
            self.store.transition(j["id"], J.COMPLETED,
                                  detail="left the Windows queue after printing (sent to printer)")
        elif self.backend.usb_present() is True:
            self.store.transition(j["id"], J.COMPLETED,
                                  detail="left the Windows queue after the printer became available")
        else:
            self.store.transition(j["id"], J.UNKNOWN,
                                  detail="removed from the Windows queue while the printer was not"
                                         " ready; outcome unknown")
        self._delete_job_file(j)
        self.wake()

    def note_spooler_restart(self):
        self.last_spooler_restart = _now()
        self.invalidate_geometry()
        self.wake()

    # -------------------------------------------------------------- cleanup
    def _delete_job_file(self, job: dict):
        for part in self.store.parts(job):
            name = part.get("stored_name")
            if not name:
                continue
            try:
                (self.cfg.jobs_dir / name).unlink(missing_ok=True)
            except OSError:
                log.warning("could not delete job file %s", name, exc_info=True)

    def _cleanup_orphans(self):
        known = {p.get("stored_name") for j in self.store.by_status(J.ACTIVE)
                 for p in self.store.parts(j)}
        for f in self.cfg.jobs_dir.iterdir():
            if f.is_file() and f.name not in known:
                f.unlink(missing_ok=True)
        uploads = {r["stored_name"] for r in self.store.db.query("SELECT stored_name FROM uploads")}
        for f in self.cfg.uploads_dir.iterdir():
            if f.is_file() and f.name not in uploads:
                f.unlink(missing_ok=True)

    def cleanup_once(self):
        s = self.settings.all()
        db = self.store.db
        cutoff = (_now() - timedelta(minutes=s["upload_ttl_minutes"])).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ")
        for r in db.query("SELECT id, stored_name FROM uploads WHERE created_at < ?", (cutoff,)):
            (self.cfg.uploads_dir / r["stored_name"]).unlink(missing_ok=True)
            db.execute("DELETE FROM uploads WHERE id = ?", (r["id"],))
        active = {p.get("stored_name") for j in self.store.by_status(J.ACTIVE)
                  for p in self.store.parts(j)}
        stale = time.time() - 600
        for f in self.cfg.jobs_dir.iterdir():
            # Safety net for files of finished jobs / abandoned copies. The age check avoids
            # racing a job that is being created (files are copied before the DB record).
            if f.is_file() and f.name not in active and f.stat().st_mtime < stale:
                f.unlink(missing_ok=True)
        job_cut = (_now() - timedelta(days=s["job_retention_days"])).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ")
        removed = self.store.delete_older_than(job_cut)
        if removed:
            log.info("Retention: deleted %d old job record(s)", len(removed))
        audit_cut = (_now() - timedelta(days=s["audit_retention_days"])).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ")
        self.audit.purge_older_than(audit_cut)
        sim = self.cfg.sim_output_dir
        if sim.exists():
            old = time.time() - 86400
            for d in sim.iterdir():
                if d.is_dir() and d.stat().st_mtime < old:
                    shutil.rmtree(d, ignore_errors=True)

    def _cleanup_loop(self):
        while not self._stop.wait(300):
            try:
                self.cleanup_once()
            except Exception:
                log.exception("cleanup failed")
