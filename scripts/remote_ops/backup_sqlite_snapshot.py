#!/usr/bin/env python3
"""Create a consistent SQLite snapshot and verify its referenced uploads are present.

Exit 0 means the snapshot passed integrity_check and every current uploads-table row
has a corresponding file in the staged uploads directory. Exit 21 means a referenced
upload was not copied; the caller may re-copy the data directory and retry.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from urllib.parse import quote


def sqlite_uri(path: Path) -> str:
    return "file:" + quote(path.resolve().as_posix(), safe="/:") + "?mode=ro"


def main() -> int:
    if len(sys.argv) != 4:
        print("usage: backup_sqlite_snapshot.py SOURCE_DB DEST_DB STAGED_DATA_DIR", file=sys.stderr)
        return 2

    source = Path(sys.argv[1])
    dest = Path(sys.argv[2])
    staged_data = Path(sys.argv[3])
    if not source.is_file():
        print(f"source database not found: {source}", file=sys.stderr)
        return 2

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.unlink(missing_ok=True)

    src = sqlite3.connect(sqlite_uri(source), uri=True, timeout=30)
    dst = sqlite3.connect(dest, timeout=30)
    try:
        src.backup(dst)
        result = dst.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            print(f"SQLite integrity_check failed: {result!r}", file=sys.stderr)
            return 3

        tables = {row[0] for row in dst.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        missing: list[str] = []
        if "uploads" in tables:
            columns = {row[1] for row in dst.execute("PRAGMA table_info(uploads)")}
            if "stored_name" in columns:
                for (stored_name,) in dst.execute("SELECT stored_name FROM uploads"):
                    if not stored_name:
                        continue
                    candidate = staged_data / "uploads" / Path(str(stored_name)).name
                    if not candidate.is_file():
                        missing.append(str(stored_name))
        if "jobs" in tables:
            columns = {row[1] for row in dst.execute("PRAGMA table_info(jobs)")}
            if {"status", "stored_name"}.issubset(columns):
                active_states = {"RECEIVED", "VALIDATING", "QUEUED", "RENDERING", "SUBMITTED", "PRINTING", "WAITING"}
                select_columns = ["status", "stored_name"]
                has_parts = "parts_json" in columns
                if has_parts:
                    select_columns.append("parts_json")
                query = "SELECT " + ", ".join(select_columns) + " FROM jobs"
                import json
                for row in dst.execute(query):
                    status = str(row[0] or "").upper()
                    if status not in active_states:
                        continue
                    names: list[str] = []
                    if row[1]:
                        names.append(str(row[1]))
                    if has_parts and row[2]:
                        try:
                            parts = json.loads(row[2])
                            if isinstance(parts, list):
                                names.extend(str(part.get("stored_name")) for part in parts
                                             if isinstance(part, dict) and part.get("stored_name"))
                        except (TypeError, ValueError):
                            print("ACTIVE_JOB_PARTS_INVALID", file=sys.stderr)
                            return 3
                    for name in names:
                        candidate = staged_data / "jobs" / Path(name).name
                        if not candidate.is_file():
                            missing.append("jobs/" + Path(name).name)

        if missing:
            for name in missing[:50]:
                print(f"MISSING_DATA_FILE:{name}", file=sys.stderr)
            print(f"snapshot has {len(missing)} active upload/job reference(s) without copied files", file=sys.stderr)
            return 21

        print("ok")
        return 0
    finally:
        dst.close()
        src.close()


if __name__ == "__main__":
    raise SystemExit(main())
