"""Persistent print-job records and state transitions.

States (spec §26 / prompt):
  RECEIVED -> VALIDATING -> QUEUED -> RENDERING -> SUBMITTED -> PRINTING/WAITING -> COMPLETED
  failure/cancel: FAILED, CANCELLED; UNKNOWN when the spooler lost the job and the
  outcome cannot be observed (e.g. spooler restart).

"SUBMITTED" means EndDoc returned (the job is in the Windows queue). It is NOT
reported as printed. "COMPLETED" means the job was observed leaving the Windows
queue after printing — the spooler finished sending it to the printer.
"""
from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timedelta, timezone

from .db import Database, utcnow
from .options import PrintOptions

log = logging.getLogger("lps.jobs")

RECEIVED, VALIDATING, QUEUED, RENDERING = "RECEIVED", "VALIDATING", "QUEUED", "RENDERING"
SUBMITTED, PRINTING, WAITING = "SUBMITTED", "PRINTING", "WAITING"
COMPLETED, FAILED, CANCELLED, UNKNOWN = "COMPLETED", "FAILED", "CANCELLED", "UNKNOWN"

ALL_STATES = [RECEIVED, VALIDATING, QUEUED, RENDERING, SUBMITTED, PRINTING, WAITING,
              COMPLETED, FAILED, CANCELLED, UNKNOWN]
PRE_SPOOL = {RECEIVED, VALIDATING, QUEUED}
IN_SPOOLER = {SUBMITTED, PRINTING, WAITING}
ACTIVE = PRE_SPOOL | {RENDERING} | IN_SPOOLER
FINAL = {COMPLETED, FAILED, CANCELLED, UNKNOWN}

_TS_FIELD = {QUEUED: "queued_at", RENDERING: "render_started_at", SUBMITTED: "submitted_at",
             PRINTING: "printing_started_at", COMPLETED: "finished_at", FAILED: "finished_at",
             CANCELLED: "finished_at", UNKNOWN: "finished_at"}

_UPDATABLE = {"status_detail", "error_code", "error_message", "spooler_job_id", "spooler_doc_name",
              "spooler_seen_printing", "cancel_requested", "attempts", "next_attempt_at",
              "sheet_count", "stored_name", "queued_at", "printing_started_at"}


def iso_in(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ")


class JobStore:
    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------------ create
    def create(self, *, user_id, username, owner_key, client_ip, opts: PrintOptions,
               parts: list[dict] | None = None, filename=None, doc_type=None, stored_name=None,
               file_size=None, page_count=None) -> str:
        """`parts`: one dict per document in print order
        {filename, doc_type, stored_name, file_size, page_count}. The single-file keyword
        arguments are kept for callers that create one-document jobs (e.g. test page)."""
        if parts is None:
            parts = [{"filename": filename, "doc_type": doc_type, "stored_name": stored_name,
                      "file_size": file_size, "page_count": page_count}]
        sel = opts.documents or [{"page_range": opts.page_range, "pages": opts.pages}]
        parts = [{**p, "page_range": d.get("page_range", ""), "selected": len(d.get("pages") or [])}
                 for p, d in zip(parts, sel)]
        filename = parts[0]["filename"] if len(parts) == 1 else             f"{parts[0]['filename']} (+{len(parts) - 1})"
        doc_type = parts[0]["doc_type"] if len(parts) == 1 else "multi"
        stored_name = parts[0]["stored_name"] if len(parts) == 1 else None
        file_size = sum(p.get("file_size") or 0 for p in parts)
        page_count = sum(p.get("page_count") or 0 for p in parts)
        pages_selected = " | ".join(p["page_range"] or "*" for p in parts)
        if pages_selected == "*":
            pages_selected = ""
        job_id = secrets.token_hex(6)
        now = utcnow()
        with self.db.transaction() as c:
            seq = c.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM jobs").fetchone()[0]
            c.execute(
                """INSERT INTO jobs(id, seq, user_id, username, owner_key, client_ip, filename,
                   doc_type, stored_name, file_size, page_count, pages_selected, options_json,
                   paper, media, color, quality, orientation, scaling, margins_mm, copies,
                   collated, reverse_order, nup, borderless, mirror, rotate180,
                   status, created_at, updated_at, parts_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (job_id, seq, user_id, username, owner_key, client_ip, filename, doc_type,
                 stored_name, file_size, page_count, pages_selected,
                 json.dumps(opts.to_dict()),
                 opts.paper if opts.paper != "CUSTOM"
                 else f"CUSTOM {opts.custom_w_mm:g}x{opts.custom_h_mm:g}",
                 opts.media, opts.color, opts.quality, opts.orientation, opts.scaling,
                 opts.margins_mm, opts.copies, int(opts.collate), int(opts.reverse), opts.nup,
                 int(opts.borderless), int(opts.mirror), int(opts.rotate180), RECEIVED, now, now,
                 json.dumps(parts)))
        return job_id

    # ----------------------------------------------------------------- queries
    def get(self, job_id: str) -> dict | None:
        r = self.db.one("SELECT * FROM jobs WHERE id = ?", (job_id,))
        return dict(r) if r else None

    @staticmethod
    def parts(job: dict) -> list[dict]:
        """Documents of a job in print order (v1.0 jobs: the single stored file)."""
        if job.get("parts_json"):
            return json.loads(job["parts_json"])
        return [{"filename": job["filename"], "doc_type": job["doc_type"],
                 "stored_name": job["stored_name"], "file_size": job["file_size"],
                 "page_count": job["page_count"], "page_range": job.get("pages_selected") or "",
                 "selected": None}]

    def options(self, job: dict) -> PrintOptions:
        return PrintOptions.from_dict(json.loads(job["options_json"]))

    def search(self, *, owner_key=None, username=None, status=None, text=None, job_id=None,
               date_from=None, date_to=None, limit=50, offset=0) -> tuple[list[dict], int]:
        where, params = [], []
        if owner_key:
            where.append("owner_key = ?"); params.append(owner_key)
        if username:
            where.append("username LIKE ?"); params.append(f"%{username}%")
        if status:
            if status == "ACTIVE":
                where.append(f"status IN ({','.join('?' * len(ACTIVE))})"); params += sorted(ACTIVE)
            else:
                where.append("status = ?"); params.append(status)
        if text:   # also matches file names inside multi-document jobs
            where.append("(filename LIKE ? OR parts_json LIKE ?)"); params += [f"%{text}%"] * 2
        if job_id:
            where.append("(id LIKE ? OR CAST(spooler_job_id AS TEXT) = ?)")
            params += [f"{job_id}%", job_id]
        if date_from:
            where.append("created_at >= ?"); params.append(date_from)
        if date_to:
            where.append("created_at < ?"); params.append(date_to)
        cond = (" WHERE " + " AND ".join(where)) if where else ""
        total = self.db.one(f"SELECT COUNT(*) AS n FROM jobs{cond}", params)["n"]
        rows = self.db.query(f"SELECT * FROM jobs{cond} ORDER BY seq DESC LIMIT ? OFFSET ?",
                             params + [limit, offset])
        return [dict(r) for r in rows], total

    def by_status(self, statuses) -> list[dict]:
        statuses = list(statuses)
        rows = self.db.query(f"SELECT * FROM jobs WHERE status IN ({','.join('?' * len(statuses))})"
                             " ORDER BY seq", statuses)
        return [dict(r) for r in rows]

    def count_active(self, owner_key: str | None = None) -> int:
        sql = f"SELECT COUNT(*) AS n FROM jobs WHERE status IN ({','.join('?' * len(ACTIVE))})"
        params = sorted(ACTIVE)
        if owner_key:
            sql += " AND owner_key = ?"
            params.append(owner_key)
        return self.db.one(sql, params)["n"]

    def counts(self) -> dict[str, int]:
        return {r["status"]: r["n"] for r in
                self.db.query("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")}

    def last_with_status(self, status: str) -> dict | None:
        r = self.db.one("SELECT * FROM jobs WHERE status = ? ORDER BY finished_at DESC LIMIT 1",
                        (status,))
        return dict(r) if r else None

    # ------------------------------------------------------------- transitions
    def transition(self, job_id: str, new_status: str, *, expect=None, detail=None,
                   error_code=None, error_message=None, **fields) -> bool:
        """Atomic state change. `expect` limits the allowed current states."""
        sets = ["status = ?", "updated_at = ?"]
        now = utcnow()
        params: list = [new_status, now]
        ts = _TS_FIELD.get(new_status)
        if ts and ts not in fields:
            sets.append(f"{ts} = COALESCE({ts}, ?)" if new_status == PRINTING else f"{ts} = ?")
            params.append(now)
        if detail is not None or new_status in FINAL:
            sets.append("status_detail = ?"); params.append(detail)
        if error_code is not None:
            sets.append("error_code = ?"); params.append(error_code)
        if error_message is not None:
            sets.append("error_message = ?"); params.append(error_message)
        for k, v in fields.items():
            if k not in _UPDATABLE:
                raise ValueError(k)
            sets.append(f"{k} = ?"); params.append(v)
        sql = f"UPDATE jobs SET {', '.join(sets)} WHERE id = ?"
        params.append(job_id)
        if expect:
            expect = [expect] if isinstance(expect, str) else list(expect)
            sql += f" AND status IN ({','.join('?' * len(expect))})"
            params += expect
        ok = self.db.execute(sql, params).rowcount == 1
        if ok:
            log.info("job %s -> %s%s", job_id, new_status, f" ({detail})" if detail else "")
        return ok

    def update(self, job_id: str, **fields):
        for k in fields:
            if k not in _UPDATABLE:
                raise ValueError(k)
        sets = ", ".join(f"{k} = ?" for k in fields) + ", updated_at = ?"
        self.db.execute(f"UPDATE jobs SET {sets} WHERE id = ?",
                        list(fields.values()) + [utcnow(), job_id])

    def claim_next(self) -> dict | None:
        """Atomically move the oldest runnable QUEUED job to RENDERING."""
        now = utcnow()
        with self.db.transaction() as c:
            r = c.execute("SELECT id FROM jobs WHERE status = ? AND cancel_requested = 0 AND"
                          " (next_attempt_at IS NULL OR next_attempt_at <= ?) ORDER BY seq LIMIT 1",
                          (QUEUED, now)).fetchone()
            if not r:
                return None
            c.execute("UPDATE jobs SET status = ?, render_started_at = ?, updated_at = ?,"
                      " status_detail = NULL WHERE id = ?", (RENDERING, now, now, r["id"]))
        log.info("job %s -> RENDERING", r["id"])
        return self.get(r["id"])

    def recover_after_restart(self) -> list[str]:
        """Jobs interrupted mid-render cannot be safely resumed (a partial spooler
        job may exist), so they fail with a clear message instead of double printing."""
        affected = []
        for j in self.by_status([RECEIVED, VALIDATING, RENDERING]):
            self.transition(j["id"], FAILED, error_code="err.server_restarted",
                            detail="server restarted while the job was being processed")
            affected.append(j["id"])
        return affected

    def delete_older_than(self, cutoff_iso: str) -> list[dict]:
        rows = [dict(r) for r in self.db.query(
            f"SELECT id, stored_name FROM jobs WHERE created_at < ? AND status IN"
            f" ({','.join('?' * len(FINAL))})", [cutoff_iso] + sorted(FINAL))]
        if rows:
            self.db.execute(f"DELETE FROM jobs WHERE id IN ({','.join('?' * len(rows))})",
                            [r["id"] for r in rows])
        return rows


def public_job(job: dict, include_admin: bool = False) -> dict:
    """Job fields safe to send to browsers (no filesystem paths)."""
    keys = ["id", "seq", "username", "filename", "doc_type", "page_count", "pages_selected",
            "sheet_count", "paper", "media", "color", "quality", "orientation", "scaling",
            "margins_mm", "copies", "collated", "reverse_order", "nup", "borderless", "mirror",
            "rotate180", "status", "status_detail", "error_code", "spooler_job_id",
            "cancel_requested", "created_at", "queued_at", "render_started_at", "submitted_at",
            "printing_started_at", "finished_at", "updated_at"]
    if include_admin:
        keys += ["client_ip", "error_message", "attempts", "file_size", "spooler_doc_name"]
    out = {k: job.get(k) for k in keys}
    # Per-document summary for history views (never storage names / paths).
    out["parts"] = [{"filename": p.get("filename"), "doc_type": p.get("doc_type"),
                     "page_count": p.get("page_count"), "page_range": p.get("page_range") or "",
                     "selected": p.get("selected")} for p in JobStore.parts(job)]
    out["cancellable"] = job["status"] in ACTIVE and not job.get("cancel_requested")
    return out
