# LocalPrintServer (LPS) — Windows Edition 🖨️

A self-hosted, mobile-ready LAN print server and print queue management system for Windows, engineered specifically around the Windows Print Spooler (`win32print` / GDI) and the **EPSON L1800** wide-format photo printer.

LocalPrintServer frees small office and educational LANs from proprietary client-side driver installations, Windows Session 0 service isolation hurdles, and manual print handling. Users upload documents from any desktop or phone browser on the local network, preview the exact physical sheet layout before printing, and track their jobs in real time.

---

## Key Features

### Document Pipeline & Layout Engine
- **Broad Format Support:** Native PDF rendering (`PyMuPDF`), images (`JPEG`, `PNG`, `BMP`, `GIF`, `TIFF`, `WEBP` via `Pillow` with EXIF orientation & alpha composition), and Microsoft Office documents (`.docx`, `.doc`, `.xlsx`, `.xls`) converted to PDF at upload time via headless LibreOffice or isolated Microsoft Office COM helper processes.
- **In-Browser Image Pre-Processing:** Built-in interactive image editor (**Cropper.js**) allowing users to crop, rotate (`±90°`), and preview images directly in the browser before upload without breaking multi-file PDF/Word uploads.
- **Multi-Document Composition & Page Selection:** Combine multiple files into a single print job, reorder documents via drag-and-drop, and pick exact page ranges (e.g. `1-5, 8, 11-13`) or toggle individual page thumbnails.
- **WYSIWYG Sheet Layout:** Live preview uses the exact same geometry engine (`lps/layout.py` + `lps/render.py`) as the physical print worker—supporting N-up (`1`, `2`, `4`, `6`, `9`, `16` pages per sheet), scaling (`fit`, `fill`, `actual_size`), margins (`normal`, `narrow`, `none`, `borderless`), orientation (`auto`, `portrait`, `landscape`), `180°` rotation, horizontal mirror, reverse page order, and collated copies.
- **One-Click Reprint:** Both users (on their isolated history page) and administrators can re-submit completed or cancelled jobs with their exact original `PrintOptions` and stored documents in a single click.

### Windows Print Spooler & Hardware Control
- **Verified GDI & DEVMODE Pipeline:** Clones a per-job `DEVMODE` structure using only verified standard fields (paper size, custom dimensions in `0.1 mm`, media type, print quality, color/monochrome, duplex/collation) and renders sheets via `CreateDC("WINSPOOL")` → `StartDoc` → `ImageWin.Dib.draw` → `EndDoc`.
- **Session 0 Hardware Maintenance:** Executes physical printer maintenance (**Nozzle Check** and **Head Cleaning**) headlessly from a Windows Service (Session 0) by extracting and timestamping official Epson `REMOTE1` ESC/P-R binary payloads (`lps/scripts/epson_maint.ps1`) and injecting them directly into `winspool.drv` as `RAW` spooler jobs.
- **Remote Spooler Job Cancellation:** Administrators can cancel active or queued print jobs from the web UI, immediately terminating them in both the Windows Print Spooler (`win32print.SetJob(..., JOB_CONTROL_CANCEL)` with PowerShell fallback) and the SQLite job state machine.
- **Evidence-Based Status & Spooler Recovery:** Monitors the Windows queue via `EnumJobs` (`lps/worker.py`, `lps/status.py`) to track `RECEIVED → QUEUED → RENDERING → SUBMITTED → PRINTING / WAITING → COMPLETED` states, automatically retrying on transient spooler errors while respecting concurrent cancellation requests.

### Security, Administration & Operations
- **Flexible Authentication Modes:** Supports full account/password login (`scrypt` password hashes), username-only identification for low-friction LAN printing, or anonymous guest sessions—while strictly requiring password-authenticated `@admin_required` sessions for `/admin/*`.
- **Strict Device & Session Isolation:** Per-browser signed HTTP-only cookies and `device_id` isolation ensure users can only view, cancel, or reprint their own jobs.
- **Live System Health & Diagnostics:** Real-time admin dashboard widget (`psutil`) displaying CPU load, RAM usage, and free disk space alongside zero-paper driver geometry verification (`lps/diagnostics.py`) and audit logging.
- **7-Day Rolling Storage Cleanup:** Automatically purges raw upload and temporary conversion files older than 7 days (`cleanup.ps1` and background worker sweep) while preserving lightweight SQLite job history records.
- **Trilingual Progressive Web App (PWA):** Complete localization in **English (`en`)**, **Russian (`ru`)**, and **Uzbek Latin (`uz`)** with installable mobile PWA manifest.

---

## Architecture

```text
Browser (Desktop / Mobile PWA)
  │  HTTP on LAN (0.0.0.0:5000, Windows Firewall limited to LocalSubnet)
  ▼
Waitress WSGI Server ─► Flask Application (lps/)
  ├─ web/auth_views, user_views, api, admin_views   Routes, CSRF, sessions, role & device checks
  ├─ options.py        Server-side validation of all print options against catalog.py
  ├─ documents.py      Magic-byte file detection, Pillow image normalization, PyMuPDF rendering,
  │                    CompositeDocument (multiple files combined into one page sequence)
  ├─ office.py         OOXML/OLE2 safety checks (rejects macros/external links/zip bombs)
  │                    + isolated PDF conversion (LibreOffice headless or MS Office COM helper)
  ├─ jobs.py           SQLite-backed job queue, multi-part job metadata & state machine
  │
  ▼  Application Queue (SQLite WAL)
worker.py ── Single Print-Worker Thread ── render.py + layout.py (shared with live preview)
  │             Selected pages → N-up grid → orientation/scaling/margins → raster sheets
  │                                        │
  │   Per-job DEVMODE copy (verified fields) ─► printer/win32_backend.py
  ▼
CreateDC('WINSPOOL') → StartDoc → StartPage → ImageWin.Dib.draw → EndPage → EndDoc
  ▼
Windows Print Spooler → EPSON L1800 Series Driver → USB Port → Printer
  ▲
worker.py ── Spooler-Monitor Thread (EnumJobs) → PRINTING / WAITING / COMPLETED / UNKNOWN
status.py ── Evidence-based printer readiness & USB PnP presence probe
```

### Repository Structure

| Path | Description |
|---|---|
| [`app.py`](app.py) | Production WSGI entry point (`waitress` server on `0.0.0.0:5000`) |
| [`manage.py`](manage.py) | Admin CLI (`create-admin`, `reset-password`, `enable-user`, `list-users`, `check`) |
| [`config.example.json`](config.example.json) | Sanitized configuration template (copy to `config.json` on deployment) |
| [`lps/catalog.py`](lps/catalog.py) | Verified Epson L1800 paper sizes, custom size bounds, media types, and quality constants |
| [`lps/options.py`](lps/options.py) | Strict server-side option validation and `PrintOptions` serialization |
| [`lps/layout.py`](lps/layout.py) & [`lps/render.py`](lps/render.py) | Sheet geometry, N-up placement, margins, mirror/rotation, and high-DPI rendering |
| [`lps/documents.py`](lps/documents.py) | Content-based file validation, PDF/image loading, and multi-document page composition |
| [`lps/office.py`](lps/office.py) & [`lps/office_helper.py`](lps/office_helper.py) | Secure Word/Excel validation and isolated subprocess conversion to PDF |
| [`lps/printer/win32_backend.py`](lps/printer/win32_backend.py) | Windows GDI/Spooler backend (`DEVMODE`, `CreateDC`, `EnumJobs`, `SetJob`, USB WMI probe) |
| [`lps/printer/sim_backend.py`](lps/printer/sim_backend.py) | Simulated printer & spooler backend for paperless development and CI testing |
| [`lps/worker.py`](lps/worker.py) | Background print worker thread, spooler monitor, retry/cancel handling, and file retention |
| [`lps/scripts/epson_maint.ps1`](lps/scripts/epson_maint.ps1) | Session 0 Epson L1800 `REMOTE1` ESC/P-R hardware maintenance utility |
| [`lps/web/`](lps/web/) | Flask blueprints (`user_views.py`, `api.py`, `admin_views.py`, `auth_views.py`) |
| [`lps/translations/`](lps/translations/) | Trilingual UI dictionaries (`en.json`, `ru.json`, `uz.json`) |
| [`scripts/`](scripts/) | Windows NSSM service installer (`install.ps1`), rollback helper (`rollback.ps1`), and encrypted backup tools |
| [`tests/`](tests/) | Comprehensive `pytest` suite (`296` tests) and PowerShell operations verification (`test_remote_ops.ps1`) |

---

## Quickstart (Development & Testing)

You can run the full application and test suite on any Windows machine without a physical printer using the built-in simulated printer and Office converter backends:

```powershell
# 1. Create and activate a virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2. Install dependencies
python -m pip install --upgrade pip
python -m pip install -r requirements.txt pytest

# 3. Run the automated test suite (uses simulated printer; prints nothing)
$env:LPS_BACKEND = "simulated"
$env:LPS_OFFICE = "simulated"
python -m pytest tests -q

# 4. Run the PowerShell syntax & backup encryption test suite
powershell -NoProfile -ExecutionPolicy Bypass -File .\tests\test_remote_ops.ps1

# 5. Start the server locally with the simulated printer backend
$env:LPS_BACKEND = "simulated"
$env:LPS_OFFICE = "simulated"
python app.py
```

When running with `LPS_BACKEND=simulated`, rendered print sheets are written as PNG images to `data\sim_output\`, and **Admin › Printer** provides controls to simulate USB disconnect/reconnect and Windows Spooler outages.

---

## Windows Production Deployment

For deploying on a Windows print-server PC with NSSM (Non-Sucking Service Manager) running under `LocalSystem`:

1. Copy [`config.example.json`](config.example.json) to `config.json` and adjust `printer_name` if needed.
2. Run [`scripts/install.ps1`](scripts/install.ps1) from an elevated PowerShell prompt to install dependencies, lock down `data\` ACLs to `SYSTEM` and `Administrators`, register the `LocalPrintServer` Windows service, and scope the Windows Firewall rule to `LocalSubnet`.
3. Create the initial administrator account via `http://127.0.0.1:5000/setup` (accessible only from localhost when no admin exists) or via CLI:
   ```powershell
   python manage.py create-admin
   ```

Detailed guides:
- **Installation, NSSM Service & Office Conversion Setup:** [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md)
- **Hardware & Spooler Acceptance Test Checklist:** [`docs/TEST_CHECKLIST.md`](docs/TEST_CHECKLIST.md)
- **Manual-Approval GitHub Deployment & Encrypted Nightly Backups:** [`docs/REMOTE_GITHUB_OPERATIONS.md`](docs/REMOTE_GITHUB_OPERATIONS.md)

---

## Security Notes

- Runtime data (`data/`), SQLite databases (`*.sqlite3`), session signing keys (`data/secret_key`), uploaded documents, logs (`logs/`), and local deployment configurations (`config.json`) are excluded from version control via [`.gitignore`](.gitignore).
- Never expose port `5000` directly to the public internet; LocalPrintServer is designed for trusted LAN segments (or access over a private VPN such as Tailscale / WireGuard).

---

## License

Released under the [MIT License](LICENSE).
