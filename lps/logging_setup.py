"""Rotating file + console logging."""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s"
_file_handler: logging.Handler | None = None


def setup_logging(log_dir: Path, level: str = "INFO") -> Path:
    """Idempotent: re-targets the file handler if called again with another directory."""
    global _file_handler
    log_file = Path(log_dir) / "app.log"
    root = logging.getLogger()
    fmt = logging.Formatter(LOG_FORMAT)
    current = getattr(_file_handler, "baseFilename", None)
    if current != str(log_file.resolve()):
        if _file_handler is not None:
            root.removeHandler(_file_handler)
            _file_handler.close()
        _file_handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
        _file_handler.setFormatter(fmt)
        root.addHandler(_file_handler)
    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
               for h in root.handlers):
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        root.addHandler(ch)
        logging.getLogger("PIL").setLevel(logging.WARNING)
    set_level(level)
    return log_file


def set_level(level: str):
    lvl = getattr(logging, str(level).upper(), logging.INFO)
    logging.getLogger().setLevel(lvl)


def tail_log(log_file: Path, max_lines: int = 500, level: str | None = None,
             search: str | None = None) -> list[str]:
    """Return the last lines of the application log, optionally filtered."""
    if not log_file.exists():
        return []
    with open(log_file, "rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        fh.seek(max(0, size - 2 * 1024 * 1024))
        data = fh.read().decode("utf-8", errors="replace")
    lines = data.splitlines()
    if level:
        order = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        if level.upper() in order:
            allowed = set(order[order.index(level.upper()):])
            out, keep = [], False
            for ln in lines:
                parts = ln.split(None, 3)
                if len(parts) >= 3 and parts[2] in order:
                    keep = parts[2] in allowed
                if keep:
                    out.append(ln)
            lines = out
    if search:
        s = search.lower()
        lines = [ln for ln in lines if s in ln.lower()]
    return lines[-max_lines:]
