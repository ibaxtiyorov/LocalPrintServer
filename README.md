# LocalPrintServer — LAN print management for the EPSON L1800

A LAN web application for the Windows PC that has the EPSON L1800 on USB001.
Users upload documents from any device on the LAN, pick exactly which pages to print,
combine several documents into one job, preview the final sheets, choose print
settings (or a saved preset) and follow their jobs. Administrators manage the queue,
printer, users, settings and logs.

**Supported files:** PDF · JPEG, PNG, BMP, GIF, TIFF, WEBP · Word `.docx` `.doc` ·
Excel `.xlsx` `.xls` (Word/Excel need Microsoft Office or LibreOffice on the server).

**Main features (v1.1):** page selection by clickable thumbnails or ranges
(`2,5-8,15,20-22`), several documents per job in a drag-and-drop order, N-up 1/2/4/6/9/16,
copies with collation, reverse order, mirror/180°, paper/media/quality/colour/orientation/
scaling/margins, print presets (personal and shared), job history with files, selected
pages and settings, password or username-only login for printing (admin always needs a
password), EN/RU/UZ, phone-friendly.

- URL on the LAN: `http://192.168.20.15:5000`
- Deployed to `C:\LocalPrintServer`, run by the NSSM service `LocalPrintServer`
  (`python.exe app.py`, working directory `C:\LocalPrintServer`)
- Languages: English, Russian, Uzbek (Latin)
- Source of truth for printer behaviour: *LocalPrintServer — EPSON L1800 Final
  Developer Master Specification v1.0*. Every printer value in the code comes from it.

Deployment: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).
Acceptance tests: [docs/TEST_CHECKLIST.md](docs/TEST_CHECKLIST.md).

## Architecture

```
Browser (PC / phone)
  │  HTTP, LAN only (0.0.0.0:5000, firewall: local subnet)
  ▼
waitress ─► Flask app (lps/)
  ├─ web/auth_views, user_views, api, admin_views   routes, CSRF, sessions, authorization
  ├─ options.py        server-side validation of every print option
  ├─ documents.py      upload type detection (magic bytes), Pillow images, PyMuPDF PDFs,
  │                    CompositeDocument = several documents as ONE page sequence
  ├─ office.py         Word/Excel: content checks, then conversion to PDF at upload time
  │                    (Microsoft Office COM or LibreOffice, separate process, timeout)
  ├─ jobs.py           persistent job records (one part per document) + state machine
  │
  ▼  application queue (DB)
worker.py ── ONE print-worker thread ── render.py + layout.py (same code as the preview)
  │             selected pages only → N-up grid → orientation/scaling/margins → sheets
  │                                        │
  │   per-job DEVMODE copy (standard fields only) ─ printer/win32_backend.py
  ▼
CreateDC('WINSPOOL') → StartDoc → StartPage → ImageWin.Dib.draw → EndPage → EndDoc
  ▼
Windows Print Spooler → EPSON L1800 driver → USB001 → printer
  ▲
worker.py ── spooler-monitor thread (EnumJobs) → PRINTING / WAITING / COMPLETED / UNKNOWN
status.py ── evidence-based printer state (never "ready" just because Status == 0)
```

| Module | Responsibility |
|---|---|
| `lps/catalog.py` | Verified Epson values: paper sizes (§6), custom limits (§7), media types (§8), quality (§9), colour/orientation (§5, §10, §11) |
| `lps/options.py` | Validates browser input; raw DEVMODE/DriverData is never accepted |
| `lps/layout.py` | Pure geometry: actual/fit/fill, margins, N-up grids 1/2/4/6/9/16, mirror, 180°, auto-orientation, page order, copies order |
| `lps/office.py`, `office_helper.py`, `office_samples.py` | Word/Excel detection, safety checks, conversion engines, Office COM helper process, sample files for the admin test |
| `lps/render.py` | Turns pages into images for each placement; builds the preview |
| `lps/documents.py` | Upload validation, EXIF transpose, alpha→white, PDF rendering (PyMuPDF, thread-locked), multi-document page sequence |
| `lps/printer/win32_backend.py` | DEVMODE, GetDeviceCaps, StartDoc…EndDoc, EnumJobs, SetJob, pause/resume/purge, spooler restart, driver info, capabilities, USB probe |
| `lps/printer/sim_backend.py` | Simulator for development/tests (renders sheets to PNG, fake spooler) |
| `lps/worker.py` | Single print worker, spooler monitor, retries, cancellation, cleanup/retention |
| `lps/status.py` | Printer state from observable evidence |
| `lps/auth.py`, `security.py` | Users/roles, scrypt hashes, sessions, CSRF, login rate limiting, headers |
| `lps/audit.py`, `settings_service.py`, `logging_setup.py`, `i18n.py`, `config.py`, `db.py` | Audit log, runtime settings, logs, translations, config, SQLite |

## How the verified printer facts are used

| Spec | Implementation |
|---|---|
| §4 verified GDI path | `Win32Session`: exactly CreateDC → CreateDCFromHandle → StartDoc → StartPage → `ImageWin.Dib.draw` → EndPage → EndDoc → DeleteDC. The Windows job id comes from `win32print.StartDoc` (same GDI call) with document-name matching as fallback. |
| §5–§11 DEVMODE values | `catalog.py` + `build_device_settings`; written to **public DEVMODE fields only** on a fresh copy of the printer default for each job. `SetPrinter` is never called with a DEVMODE, so one job cannot change the defaults or another job. |
| §13 printable area | Every job reads HORZRES/VERTRES/PHYSICAL*/LOGPIXELS from its own DC; nothing is hard-coded. The preview asks the driver for the same geometry (a DC with no StartDoc creates no job) and falls back to an estimate that is clearly labelled. |
| §14–§15 borderless / expansion | Offered only when (a) enabled by an admin and (b) the installed driver itself advertises a matching "(Borderless)" paper size through `DeviceCapabilities`. The expansion slider and private bytes are **not** used. |
| §16 High Speed | Not exposed (no safe control). |
| §17 mirror / rotate 180 | Done by the application on the rendered page (byte 1288 is shared private state). |
| §18 N-up | Composed by the application (2 or 4 per sheet). |
| §19–§20 copies / collate | Standard `DEVMODE.Copies` / `Collate` (default). Admin can switch to application-side copies. |
| §21 reverse order | Application re-orders sheets. |
| §22 paper source | No feed selector. |
| §24 PDF | PyMuPDF renders each page to an image, which then goes through the same image pipeline. |
| §25 status | `Status == 0` is never shown as "ready"; state comes from spooler, queue, job flags, stall detection and (supplementary) USB presence. |
| §26 job states | `SUBMITTED` = EndDoc returned; `COMPLETED` only after the job was seen leaving the Windows queue. |
| §27 spooler recovery | Worker retries while the spooler is down; jobs lost by a restart become `UNKNOWN` instead of being reported as printed. |
| §28 concurrency | One worker thread + backend lock serialise every DC operation. |

## Development

```
python -m venv .venv --system-site-packages
.venv\Scripts\pip install -r requirements.txt pytest
.venv\Scripts\python -m pytest            # uses the simulated printer, prints nothing
set LPS_BACKEND=simulated & set LPS_OFFICE=simulated & set LPS_PORT=5055 & .venv\Scripts\python app.py
```
`LPS_OFFICE=simulated` converts Word/Excel files without any Office software (one page per
Word page break / Excel sheet). Tests `test_real_libreoffice_conversion` and
`test_real_microsoft_office_conversion` run automatically where that software is installed.
With the simulator, printed sheets are written as PNG files to `data\sim_output\`, and
Admin › Printer has buttons to simulate USB unplug/reconnect and spooler stop/start.

## Remaining limitations (honest list)

1. **Physical verification pending.** This package was built on a development PC without
   the Epson. DEVMODE values are exactly the verified ones, but the first run on the
   server must complete the hardware steps in `docs/TEST_CHECKLIST.md` (section H).
2. **"Completed" means "left the Windows queue after printing"**, i.e. the spooler finished
   sending data to the printer. Windows gives no reliable physical "paper came out" signal.
   A job deleted directly in the Windows queue UI (not through this website) may be shown
   as completed or unknown.
3. **Printer readiness is inferred.** The Epson reports Status 0 even when disconnected.
   The USB-presence check (Plug and Play, vendor 04B8) is supplementary and must be
   confirmed on the server; it can be turned off in `config.json`.
4. **Borderless** is available only if the installed driver advertises separate borderless
   paper sizes. If the driver implements borderless only through its private settings (as the
   expansion observations suggest), borderless stays hidden — by design (spec §15, §36).
5. **Borderless expansion level, High Speed, and driver-side mirror/rotate/N-up/reverse** are
   not controlled (private DriverData). Mirror, 180°, N-up and reverse are done by the app.
6. **Windows printer Properties/Preferences dialogs** cannot open on a phone or a second PC.
   They open only on the server's own desktop and only when the app is not running as a
   service; otherwise the admin page shows the exact command to run on the server.
7. **Media/quality combinations** are passed to the driver exactly as requested, following the
   verified path of spec §4 (no `DocumentProperties` step; setting "Let the driver validate
   settings" is off by default). Whether the Epson honours MediaType/PrintQuality from the
   public fields alone is confirmed on site with Admin › Printer › Driver check (no paper).
   If the setting is switched on, any adjustment made by the driver is logged
   (`Driver adjusted DEVMODE fields`).
8. **HTTP only.** The site is for the LAN. Cookies are HttpOnly + SameSite=Lax but not
   `Secure` (no TLS). Do not expose port 5000 to the internet.
9. **Login rate limiting is in memory** (resets when the service restarts).
10. Only the first frame of GIF / multi-page TIFF files is printed.
11. **Word/Excel fidelity depends on the converter.** Microsoft Office gives the layout users
    see in Word/Excel; LibreOffice is very close but can differ in fonts, line breaks and
    therefore page breaks. Fonts used in a document must be installed on the server.
    Pagination is fixed at upload time (the page thumbnails show exactly what will print).
12. **Microsoft Office inside a Windows service is not officially supported by Microsoft.**
    It was verified here only in an interactive session (Word 3 pages in 6 s, Excel 2 pages in
    64 s with a slow network default printer). Under the NSSM service (LocalSystem) it must be
    confirmed on site with Admin › Printer › Test conversion; if it hangs or fails, set
    `"office_converter": "libreoffice"` (LibreOffice was verified working here, ~10 s per file).
13. **Rejected Word/Excel files:** macro-enabled, password-protected and documents that link
    to external files/templates (security), and files over the page/size limits. Users are
    told to save such files as PDF.
14. **Username-only mode trusts the typed name.** Anyone can type any name; it is a label for
    history, not authentication. Use password mode where accountability matters. The admin
    panel always requires a password account.
15. Page thumbnails are generated on demand (lazy loading); very long documents (hundreds of
    pages) are selectable but scrolling the thumbnail grid loads them progressively.
