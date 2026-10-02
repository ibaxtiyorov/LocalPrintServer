# Acceptance test checklist

Legend: **[auto]** covered by the automated test suite (simulated printer, no paper) ·
**[server]** must be done on the print-server PC · 📄 uses paper (keep to the minimum listed).

Physical behaviour already verified in the specification (image path, paper/media/quality
values, custom 200×250, actual-size 100 mm, copies+collate+reverse order, USB reconnect prints
queued job, spooler restart) is **not** repeated unless listed below.

## 0. Automated suite (development PC, repeatable on the server)

```powershell
cd C:\LocalPrintServer
& $PY -m pip install pytest
& $PY -m pytest -q          # simulated backend; prints nothing
```

Covers: layout maths (actual/fit/fill, margins, N-up, mirror, 180°, auto-orientation,
reverse, copies order) · every verified DEVMODE value (§5–§11) · DEVMODE writes only
public fields, never `SetPrinter`, never touches DriverData · borderless detection from
driver paper names · option validation incl. custom range §7, B3/A2 rejected · upload type
detection by content, encrypted/corrupt PDFs, EXIF rotation, oversized/empty files,
path-traversal names · end-to-end jobs through the simulated spooler with the output sheets
inspected (paper size, landscape, custom, mirror, 180°, mono, 100 mm actual size, PDF
multi-page, page range, N-up + reverse, driver vs application copies) · states
RECEIVED→…→SUBMITTED→COMPLETED, never "completed" straight from EndDoc · cancellation
(queued / in Windows queue) · USB unplug → WAITING → reconnect → COMPLETED · spooler down →
retry → print · spooler restart → UNKNOWN + recovery · 6 concurrent submissions from two users
keep their own settings · user isolation · login, rate limiting, CSRF, role checks, session
invalidation on disable/password reset, last-admin protection, no passwords in DB/logs/audit,
no tracebacks to users · translations complete in EN/RU/UZ.

v1.1 additions: page-range parsing/validation (`1-5`, `1,3,7`, `2,5-8,15,20-22`, against the
real page count) · only selected pages reach the printer (checked by page colour on the printed
sheets) · several documents in a chosen order · N-up 1/2/4/6/9/16 · copies with/without
collation and reverse order · cancellation and file clean-up of multi-document jobs · Word/Excel
detection by content, rejection of macro/encrypted/externally-linked/zip-bomb files, simulated
conversion end to end, exact converter command lines (no shell, fixed file name, isolated
profile), timeout kill and temp clean-up, >260-character paths · presets (private/shared) ·
username-only mode (never grants admin) · thumbnails · history. Where LibreOffice or Microsoft
Office is installed, a real conversion test runs as well.

## 1. Installation [server]

| # | Step | Expected |
|---|---|---|
| 1.1 | Run `scripts\install.ps1` as Administrator | Backup folder created; "Service is answering on port 5000" |
| 1.2 | `manage.py check` output (printed by installer) | pywin32/Pillow/PyMuPDF/waitress OK; printer snapshot `exists: True`; spooler running |
| 1.3 | `http://127.0.0.1:5000/setup` on the server | Create admin works; the same URL from another PC shows "only from the server" |

## 2. Driver verification without paper [server]

| # | Step | Expected |
|---|---|---|
| 2.1 | Admin › Printer › **Driver check** | Every verified paper size: DC size matches nominal (± a few mm; custom 200×250 shown with its geometry). Any "driver changed" entries for media/quality combinations are listed — **note them** |
| 2.2 | Admin › Printer › "Paper sizes: verified vs driver" | All ✔. If the driver lists "(Borderless)" sizes they appear in the Borderless column |
| 2.3 | Admin › Printer › Printer & driver | Port USB001, driver EPSON L1800 Series, driver version as in spec §2 |

2.1 runs with "Let the driver validate settings" **off** (default = the verified path of spec §4).
Then switch it **on** in Admin › Settings, run 2.1 again and compare: if the driver changes
PrintQuality/MediaType or paper size only in one mode, keep the mode that leaves the requested
values intact (normally **off**) and record both results.

## 3. Queue, states and cancellation without paper [server]

Prepare: **switch the printer off** (or unplug USB) so nothing can print.

| # | Step | Expected |
|---|---|---|
| 3.1 (C) | Upload a JPEG, print | Job: Queued → Preparing → Sent to Windows queue → (after `stall_seconds`, default 90 s) **Waiting for printer** with a reason. Admin › Printer › Windows print queue shows the same Windows job id as the job row (exact EnumJobs match) |
| 3.2 | Header status | Not "Ready"; shows Waiting/Printer problem. Evidence panel shows Windows status 0 and explains it is not trusted |
| 3.3 (B) | Cancel the job from "My jobs" | "Cancellation requested" → **Cancelled**; job disappears from the Windows queue |
| 3.4 | Pause application queue (Maintenance), submit a job | Job stays **Queued**; cancel → Cancelled immediately; nothing reaches Windows queue |
| 3.5 | Resume application queue | — |
| 3.6 | Invalid inputs: `.exe` renamed to `.pdf`; empty file; password-protected PDF; > max size; custom 50×50 mm | Clear messages, no job created, nothing in logs beyond one INFO/WARNING line each |

## 4. Recovery [server]

| # | Step | Expected |
|---|---|---|
| 4.1 (G) | Printer still off. Submit a job → Waiting. Maintenance › **Restart Print Spooler** | Button succeeds; job becomes **Outcome unknown** (spec §27: queue was 0 after restart); audit log entry `spooler.restart` |
| 4.2 | While the spooler is stopped (`net stop spooler` in an admin console) submit a job | Status "Print service error"; job stays Queued "waiting for printer/spooler". `net start spooler` → job is sent automatically |
| 4.3 (F) 📄 1 sheet | Printer off/unplugged, submit **one A6 page in Draft on plain paper** → Waiting. Reconnect USB / power on | Job prints and becomes **Completed** (spec §25 behaviour). *Optional — skip to save paper; 3.1+3.3 already cover the waiting path* |

## 5. PDF and settings on paper [server] 📄 total 3 sheets

| # | Step | Expected |
|---|---|---|
| 5.1 (A) | Maintenance › **Print B&W test page** | A4, the square measures **100 × 100 mm**, crosses 5 mm from the edges visible where printable |
| 5.2 (A) | 4-page PDF, A4 plain, Draft, **4 per sheet**, Reverse off | One sheet: pages 1–4 left→right, top→bottom, layout identical to the preview |
| 5.3 | Same PDF page 1 only, **Mirror** on, Draft | Mirrored output matching the preview |

Not repeated (already verified in the spec): paper sizes, custom 200×250/200×290, actual size,
copies/collate/reverse physical order, colour vs B&W.

## 5a. Word/Excel, page selection, several documents [server]

Prepare: install LibreOffice and/or Microsoft Office (see DEPLOYMENT §6a). Steps W1–W5 use
no paper (switch the printer **off** and cancel the jobs); W6 uses 1 sheet.

| # | Step | Expected |
|---|---|---|
| W1 | Admin › Printer › Word / Excel conversion › **Test conversion** (running as the service) | Both DOCX and XLSX "converted with …", 2 pages each, within the time limit. If Microsoft Office hangs/fails under the service, set `"office_converter": "libreoffice"`, restart the service, repeat |
| W2 | Upload a real multi-page `.docx` and a real `.xlsx` from your office | "converted for printing"; page count equals what Word/Excel show in Print Preview; thumbnails show the real pages (tables, images, fonts) |
| W3 | Upload a `.doc` and an `.xls` (old formats); a macro file renamed `.docx`; a password-protected `.docx` | Old formats convert; macro/password files are refused with a clear message |
| W4 | One job: PDF (pages `2,5-8`) + Word (all) + photo; drag the photo to the top; 4 per sheet; copies 2 | Preview "Print order" lists exactly photo, PDF p.2, p.5–8, Word pages; the job in history shows each file with its selected pages |
| W5 | Name-only mode (Admin › Settings): sign in with a name from a phone, print, then try `/admin` | Job history shows the typed name ("name only"); `/admin` asks for a password |
| W6 📄 1 sheet | Print a 4-page Word file at **4 per sheet**, Draft, plain A4 | One sheet, pages 1–4 in reading order, layout as in the preview |

## 6. Service, reboot, LAN [server]

| # | Step | Expected |
|---|---|---|
| 6.1 | `nssm restart LocalPrintServer` | Site back within ~10 s; Admin › Diagnostics shows `windows_session: 0` (service mode) |
| 6.2 (D) | **Reboot the PC**, do not log in | From a phone: `http://192.168.20.15:5000` loads the login page |
| 6.3 | Log in on the server, close all consoles | Site keeps working (no terminal dependency) |
| 6.4 (E) | Log in as two different users on a second PC and a phone | Each sees only their own jobs; admin sees both |
| 6.5 (H) | Both devices submit 2–3 jobs at the same time (printer off) | All jobs queued in order, each with its own paper/orientation in the job list; cancel them all with "Cancel all jobs" |
| 6.6 | Kill the Python process in Task Manager | NSSM restarts it within ~5 s |
| 6.7 | Check `logs\app.log`, Admin › System log, Admin › Audit log | Useful entries for every step above; no passwords |

Record results (date, tester, pass/fail, notes) at the end of this file.

| Test | Date | Result | Notes |
|---|---|---|---|
| | | | |
