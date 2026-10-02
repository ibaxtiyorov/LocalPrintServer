"""Printer backends: real Windows spooler (win32) or development simulator."""
from __future__ import annotations

from ..config import Config
from .base import PrinterBackend, PrinterError  # noqa: F401


def create_backend(cfg: Config) -> PrinterBackend:
    if cfg["printer_backend"] == "simulated":
        from .sim_backend import SimBackend
        return SimBackend(cfg["printer_name"], cfg.sim_output_dir)
    from .win32_backend import Win32Backend
    return Win32Backend(cfg["printer_name"], cfg["usb_probe_enabled"], cfg["usb_vendor_id"])
