"""Static configuration (config.json) for LocalPrintServer.

Deployment-level values live here (host, port, printer name, paths). Values that
administrators may change at runtime live in the DB via settings_service.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

DEFAULTS: dict = {
    "host": "0.0.0.0",
    "port": 5000,
    "threads": 8,
    "printer_name": "EPSON L1800 Series",
    # "win32" talks to the real Windows spooler; "simulated" is for development
    # and automated tests only (renders sheets to PNG, fake spooler queue).
    "printer_backend": "win32",
    "data_dir": "data",
    "log_dir": "logs",
    "log_level": "INFO",
    # HTTP on a LAN: a Secure cookie would never be sent back. Set true only
    # when the site is served over HTTPS (e.g. behind a TLS reverse proxy).
    "session_cookie_secure": False,
    "session_hours": 12,
    # Supplementary USB evidence via WMI (Win32_PnPEntity, vendor 04B8 = Epson).
    # Never treated as proof of readiness; see status.py.
    "usb_probe_enabled": True,
    "usb_vendor_id": "04B8",
    # Hard ceiling for request bodies regardless of the admin setting (MB).
    "hard_max_upload_mb": 200,
    # Word/Excel -> PDF conversion engine: auto | msoffice | libreoffice | none | simulated.
    # Executable paths are deployment settings on purpose (never editable from the web UI).
    "office_converter": "auto",
    "libreoffice_path": "",
    "office_timeout_seconds": 120,
}


@dataclass
class Config:
    values: dict = field(default_factory=dict)

    def __getitem__(self, key):
        return self.values[key]

    def get(self, key, default=None):
        return self.values.get(key, default)

    @property
    def data_dir(self) -> Path:
        return _resolve(self.values["data_dir"])

    @property
    def log_dir(self) -> Path:
        return _resolve(self.values["log_dir"])

    @property
    def db_path(self) -> Path:
        return self.data_dir / "lps.sqlite3"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def sim_output_dir(self) -> Path:
        return self.data_dir / "sim_output"


def _resolve(p: str) -> Path:
    path = Path(p)
    return path if path.is_absolute() else BASE_DIR / path


def load_config(path: str | os.PathLike | None = None, overrides: dict | None = None) -> Config:
    values = dict(DEFAULTS)
    cfg_path = Path(path) if path else Path(os.environ.get("LPS_CONFIG", BASE_DIR / "config.json"))
    if cfg_path.exists():
        # utf-8-sig: Windows PowerShell 5.1 and Notepad may write a BOM.
        with open(cfg_path, "r", encoding="utf-8-sig") as fh:
            loaded = json.load(fh)
        unknown = set(loaded) - set(DEFAULTS)
        for key in unknown:
            loaded.pop(key)
        values.update(loaded)
    # Environment overrides used by development/test tooling.
    if os.environ.get("LPS_BACKEND"):
        values["printer_backend"] = os.environ["LPS_BACKEND"]
    if os.environ.get("LPS_DATA_DIR"):
        values["data_dir"] = os.environ["LPS_DATA_DIR"]
    if os.environ.get("LPS_PORT"):
        values["port"] = int(os.environ["LPS_PORT"])
    if overrides:
        values.update(overrides)
    if values["printer_backend"] not in ("win32", "simulated"):
        raise ValueError("printer_backend must be 'win32' or 'simulated'")
    if os.environ.get("LPS_OFFICE"):
        values["office_converter"] = os.environ["LPS_OFFICE"]
    cfg = Config(values)
    for d in (cfg.data_dir, cfg.log_dir, cfg.uploads_dir, cfg.jobs_dir):
        d.mkdir(parents=True, exist_ok=True)
    return cfg
