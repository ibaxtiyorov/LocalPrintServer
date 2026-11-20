from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

SNAPSHOT_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "remote_ops"
    / "backup_sqlite_snapshot.py"
)


def _create_source_db(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE uploads (id TEXT, stored_name TEXT)")
        db.execute("INSERT INTO uploads VALUES (?, ?)", ("upload-1", "document.pdf"))
        db.execute("CREATE TABLE jobs (status TEXT, stored_name TEXT, parts_json TEXT)")
        db.execute(
            "INSERT INTO jobs VALUES (?, ?, ?)",
            ("QUEUED", None, json.dumps([{"stored_name": "queued-job.pdf"}])),
        )


def _run_snapshot(source: Path, destination: Path, staged_data: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-I",
            str(SNAPSHOT_SCRIPT),
            str(source),
            str(destination),
            str(staged_data),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_snapshot_passes_integrity_and_checks_upload_and_active_job_files(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    _create_source_db(source)
    staged_data = tmp_path / "staged-data"
    (staged_data / "uploads").mkdir(parents=True)
    (staged_data / "jobs").mkdir()
    (staged_data / "uploads" / "document.pdf").write_bytes(b"uploaded")
    (staged_data / "jobs" / "queued-job.pdf").write_bytes(b"queued job")
    destination = staged_data / "lps.sqlite3"

    result = _run_snapshot(source, destination, staged_data)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout
    with sqlite3.connect(destination) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("SELECT COUNT(*) FROM uploads").fetchone()[0] == 1


def test_snapshot_fails_if_a_referenced_file_is_missing(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    _create_source_db(source)
    staged_data = tmp_path / "staged-data"
    (staged_data / "uploads").mkdir(parents=True)
    (staged_data / "jobs").mkdir()
    (staged_data / "uploads" / "document.pdf").write_bytes(b"uploaded")
    destination = staged_data / "lps.sqlite3"

    result = _run_snapshot(source, destination, staged_data)

    assert result.returncode == 21
    assert "MISSING_DATA_FILE:jobs/queued-job.pdf" in result.stderr
