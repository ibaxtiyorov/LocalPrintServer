"""Printer backend interface shared by the real Win32 backend and the simulator."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from PIL import Image

from .. import catalog
from ..layout import Geometry
from ..options import PrintOptions

# JOB_STATUS_* (winspool.h)
JOB_STATUS = {
    0x0001: "PAUSED", 0x0002: "ERROR", 0x0004: "DELETING", 0x0008: "SPOOLING",
    0x0010: "PRINTING", 0x0020: "OFFLINE", 0x0040: "PAPEROUT", 0x0080: "PRINTED",
    0x0100: "DELETED", 0x0200: "BLOCKED_DEVQ", 0x0400: "USER_INTERVENTION",
    0x0800: "RESTART", 0x1000: "COMPLETE", 0x2000: "RETAINED",
}
JS_PAUSED, JS_ERROR, JS_DELETING, JS_SPOOLING, JS_PRINTING = 0x1, 0x2, 0x4, 0x8, 0x10
JS_OFFLINE, JS_PAPEROUT, JS_PRINTED, JS_DELETED, JS_BLOCKED = 0x20, 0x40, 0x80, 0x100, 0x200
JS_USER_INTERVENTION, JS_COMPLETE = 0x400, 0x1000
JS_PROBLEM = JS_ERROR | JS_OFFLINE | JS_PAPEROUT | JS_BLOCKED | JS_USER_INTERVENTION

# PRINTER_STATUS_* (winspool.h)
PRINTER_STATUS = {
    0x1: "PAUSED", 0x2: "ERROR", 0x4: "PENDING_DELETION", 0x8: "PAPER_JAM", 0x10: "PAPER_OUT",
    0x20: "MANUAL_FEED", 0x40: "PAPER_PROBLEM", 0x80: "OFFLINE", 0x100: "IO_ACTIVE",
    0x200: "BUSY", 0x400: "PRINTING", 0x800: "OUTPUT_BIN_FULL", 0x1000: "NOT_AVAILABLE",
    0x2000: "WAITING", 0x4000: "PROCESSING", 0x8000: "INITIALIZING", 0x10000: "WARMING_UP",
    0x20000: "TONER_LOW", 0x40000: "NO_TONER", 0x80000: "PAGE_PUNT",
    0x100000: "USER_INTERVENTION", 0x200000: "OUT_OF_MEMORY", 0x400000: "DOOR_OPEN",
    0x800000: "SERVER_UNKNOWN", 0x1000000: "POWER_SAVE",
}
PS_PAUSED = 0x1
PS_PROBLEM = (0x2 | 0x8 | 0x10 | 0x40 | 0x80 | 0x800 | 0x1000 | 0x40000 | 0x100000
              | 0x200000 | 0x400000 | 0x800000)
PRINTER_ATTRIBUTE_WORK_OFFLINE = 0x400


def flag_names(value: int, table: dict[int, str]) -> list[str]:
    return [name for bit, name in table.items() if value & bit]


class PrinterError(Exception):
    """`code` is an i18n key; `retryable` marks transient spooler problems."""

    def __init__(self, code: str, detail: str = "", retryable: bool = False):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail
        self.retryable = retryable


@dataclass(frozen=True)
class DeviceSettings:
    """Everything that goes into the job's own private DEVMODE copy."""
    paper: int
    width_01mm: int
    length_01mm: int
    custom: bool
    orientation: int
    media: int
    print_quality: int
    y_resolution: int
    color: int
    copies: int
    collate: bool
    borderless: bool = False

    def cache_key(self) -> tuple:
        # Geometry does not depend on copies/collate.
        return (self.paper, self.width_01mm, self.length_01mm, self.orientation, self.media,
                self.print_quality, self.y_resolution, self.color, self.borderless)

    def to_dict(self) -> dict:
        return asdict(self)


def build_device_settings(opts: PrintOptions, orientation: str, *, driver_copies: bool,
                          borderless_papers: dict[str, int] | None = None) -> DeviceSettings:
    if opts.paper == catalog.CUSTOM_PAPER_KEY:
        paper, w, l, custom = (catalog.CUSTOM_DM_PAPER, round(opts.custom_w_mm * 10),
                               round(opts.custom_h_mm * 10), True)
    else:
        p = catalog.PAPER_BY_KEY[opts.paper]
        paper, w, l, custom = p.dm_paper, p.width_01mm, p.length_01mm, False
    if opts.borderless:
        # Only a driver-advertised borderless paper id is used (discovered via
        # DeviceCapabilities); private DriverData is never touched.
        paper = (borderless_papers or {})[opts.paper]
    pq, yr = catalog.QUALITY[opts.quality]
    return DeviceSettings(
        paper=paper, width_01mm=w, length_01mm=l, custom=custom,
        orientation=catalog.ORIENTATION[orientation],
        media=catalog.MEDIA_BY_KEY[opts.media].dm_media,
        print_quality=pq, y_resolution=yr, color=catalog.COLOR[opts.color],
        copies=opts.copies if driver_copies else 1,
        collate=opts.collate if driver_copies else False,
        borderless=opts.borderless)


@dataclass
class SpoolerJob:
    job_id: int
    document: str
    user: str
    status: int
    status_text: str
    pages_printed: int
    total_pages: int
    position: int
    submitted: str | None
    machine: str = ""
    flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class PrintSession:
    """One open printer DC. Used only from the print worker thread."""
    geometry: Geometry
    devmode_report: dict

    def start_doc(self, doc_name: str) -> int | None:
        raise NotImplementedError

    def start_page(self):
        raise NotImplementedError

    def draw(self, image: Image.Image, dest_device: tuple[int, int, int, int]):
        raise NotImplementedError

    def end_page(self):
        raise NotImplementedError

    def end_doc(self):
        raise NotImplementedError

    def abort(self):
        raise NotImplementedError

    def close(self):
        raise NotImplementedError


class PrinterBackend:
    kind = "base"
    printer_name: str

    def query_geometry(self, s: DeviceSettings, merge: bool, timeout: float | None = None) -> Geometry:
        raise NotImplementedError

    def probe(self, s: DeviceSettings, merge: bool) -> tuple[Geometry, dict]:
        """Build/validate the DEVMODE and read DC geometry without creating a job."""
        raise NotImplementedError

    def open_session(self, s: DeviceSettings, merge: bool) -> PrintSession:
        raise NotImplementedError

    def list_jobs(self) -> list[SpoolerJob]:
        raise NotImplementedError

    def cancel_spooler_job(self, job_id: int):
        raise NotImplementedError

    def printer_snapshot(self) -> dict:
        """{exists, status, status_flags, attributes, work_offline, cjobs, ...}"""
        raise NotImplementedError

    def printer_details(self) -> dict:
        raise NotImplementedError

    def capabilities(self) -> dict:
        """{'paper_ids': [...], 'paper_names': {...}, 'borderless': {key: id}, ...}"""
        raise NotImplementedError

    def set_paused(self, paused: bool):
        raise NotImplementedError

    def purge(self):
        raise NotImplementedError

    def spooler_running(self) -> bool | None:
        raise NotImplementedError

    def restart_spooler(self):
        raise NotImplementedError

    def usb_present(self) -> bool | None:
        return None

    def properties_command(self, preferences: bool) -> str:
        flag = "/e" if preferences else "/p"
        return f'rundll32 printui.dll,PrintUIEntry {flag} /n "{self.printer_name}"'

    def launch_properties(self, preferences: bool):
        raise PrinterError("err.not_supported")
