# Deployment — LocalPrintServer on the print-server PC

Target (from the specification):

| Item | Value |
|---|---|
| Server IP / URL | 192.168.20.15 → `http://192.168.20.15:5000` |
| Project folder | `C:\LocalPrintServer` |
| Python | `C:\Users\user\AppData\Local\Programs\Python\Python313\python.exe` (3.13.x) |
| NSSM | 2.24 win64, service `LocalPrintServer`, arguments `app.py`, startup dir `C:\LocalPrintServer` |
| Printer | `EPSON L1800 Series` on `USB001` |

## 1. Copy the package to the server

Copy this whole folder (without `.venv`, `data`, `logs`) to the server, e.g. to
`C:\Install\LocalPrintServer_pkg`. The server needs internet for `pip`, **or** run
`scripts\make_offline_wheels.ps1` on another PC with Python 3.13 and copy the resulting
`wheels` folder into the package first.

New Python packages: **PyMuPDF** (PDF rendering) and **waitress** (production web server).
Flask, Pillow and pywin32 are already on the server.

## 2. Install (automatic backup included)

Open **PowerShell as Administrator**:

```powershell
cd C:\Install\LocalPrintServer_pkg
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1 -RemoveStartupShortcuts
```

`nssm.exe` is found automatically from the existing `LocalPrintServer` service registration
(or PATH / `C:\nssm*`). If it is somewhere else, add `-Nssm "C:\path\to\nssm-2.24\win64\nssm.exe"`.
If Python is not at the spec path, add `-Python "C:\path\to\python.exe"`.

The script:

1. stops the service if it is running;
2. **backs up** the current `C:\LocalPrintServer` (including the old experimental `app.py`)
   and the current NSSM settings to `C:\LocalPrintServer_backups\LocalPrintServer_<timestamp>`;
3. moves any old Startup-folder launcher of `app.py` into the backup (`-RemoveStartupShortcuts`;
   without the switch it only warns) — spec §33: never run both;
4. copies the new files, keeps an existing `config.json`, creates `data\` (readable only by
   SYSTEM/Administrators) and `logs\`;
5. `pip install -r requirements.txt` into the interpreter's own site-packages (user site
   ignored), then checks the imports exactly as the LocalSystem service will see them;
6. runs `python manage.py check` (opens the printer queue and reads capabilities — prints nothing);
7. configures the NSSM service (below);
8. adds the firewall rule;
9. starts the service and checks `http://127.0.0.1:5000/login`.

## 3. Create the first administrator

Either on the server's browser: `http://127.0.0.1:5000/setup` (works **only** from the server
itself and **only** while no admin exists), or in a PowerShell **run as Administrator**
(the `data` folder is readable only by SYSTEM and Administrators):

```powershell
cd C:\LocalPrintServer
& "C:\Users\user\AppData\Local\Programs\Python\Python313\python.exe" manage.py create-admin
```

There is no default password. Then log in, open **Admin › Users** and create user accounts
(tick "Require change at next login" so users choose their own password).

**How users sign in for printing** — Admin › Settings › "How users identify themselves":

| Mode | Users | History shows |
|---|---|---|
| Account and password (default) | accounts created in Admin › Users | the account name |
| Name only (no password) | type their name (2–40 letters/digits; Cyrillic, Uzbek `Oʻ`/`O'` fine) | the typed name, marked "name only" |
| Anonymous guests | nothing | "guest-xxxx" per browser |

In every mode the **admin panel requires an account with a password**; typing an
administrator's name in name-only mode gives printing rights only.

Other CLI commands: `manage.py reset-password <user>`, `manage.py enable-user <user>`,
`manage.py list-users`, `manage.py check`.

## 4. NSSM service — equivalent manual commands

`install.ps1` runs these; they are listed for reference / manual repair:

```bat
set NSSM=C:\path\to\nssm-2.24\win64\nssm.exe
set PY=C:\Users\user\AppData\Local\Programs\Python\Python313\python.exe
%NSSM% install LocalPrintServer "%PY%" app.py
%NSSM% set LocalPrintServer Application "%PY%"
%NSSM% set LocalPrintServer AppDirectory C:\LocalPrintServer
%NSSM% set LocalPrintServer AppParameters app.py
%NSSM% set LocalPrintServer DisplayName "Local Print Server (EPSON L1800)"
%NSSM% set LocalPrintServer Start SERVICE_AUTO_START
%NSSM% set LocalPrintServer ObjectName LocalSystem
%NSSM% set LocalPrintServer AppStdout C:\LocalPrintServer\logs\service-stdout.log
%NSSM% set LocalPrintServer AppStderr C:\LocalPrintServer\logs\service-stderr.log
%NSSM% set LocalPrintServer AppRotateFiles 1
%NSSM% set LocalPrintServer AppRotateOnline 1
%NSSM% set LocalPrintServer AppRotateBytes 10485760
%NSSM% set LocalPrintServer AppExit Default Restart
%NSSM% set LocalPrintServer AppRestartDelay 5000
%NSSM% set LocalPrintServer AppStopMethodConsole 15000
%NSSM% set LocalPrintServer AppEnvironmentExtra PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8
sc failure LocalPrintServer reset= 86400 actions= restart/5000/restart/10000/restart/30000
%NSSM% start LocalPrintServer
```

Notes:

* **LocalSystem** is required for Admin › Maintenance actions that need printer
  administration rights (pause/resume/purge queue, cancel other jobs, restart the spooler).
* **Do not** add `DependOnService Spooler`: "Restart Print Spooler" uses
  `Restart-Service Spooler -Force`, which would stop dependent services — including this one.
  The app handles a missing spooler itself (jobs wait and retry).
* Service control: `nssm status|start|stop|restart LocalPrintServer`, or `services.msc`.

## 5. Firewall and LAN

The installer creates:

```powershell
New-NetFirewallRule -DisplayName "LocalPrintServer TCP 5000 (LAN)" -Direction Inbound -Action Allow `
  -Protocol TCP -LocalPort 5000 -Profile Private,Domain -RemoteAddress LocalSubnet
```

* The server's network must be **Private** (or Domain). Check with `Get-NetConnectionProfile`;
  change with `Set-NetConnectionProfile -InterfaceIndex <n> -NetworkCategory Private`.
* Do **not** forward port 5000 on the router. The site is plain HTTP for the LAN only.
* The app binds `0.0.0.0:5000` (`config.json` → `host`, `port`). Changing them requires a
  service restart (and a matching firewall rule).
* Give the server a fixed IP (DHCP reservation for 192.168.20.15) so the URL never changes.

## 6. Configuration

`C:\LocalPrintServer\config.json` (deployment values, read at start):

| Key | Default | Meaning |
|---|---|---|
| `host`, `port` | `0.0.0.0`, `5000` | Bind address |
| `printer_name` | `EPSON L1800 Series` | Windows printer queue name |
| `printer_backend` | `win32` | `simulated` only for development |
| `session_cookie_secure` | `false` | set `true` only behind HTTPS |
| `usb_probe_enabled`, `usb_vendor_id` | `true`, `04B8` | supplementary USB presence check |
| `hard_max_upload_mb` | `200` | absolute request size cap |
| `office_converter` | `auto` | Word/Excel engine: `auto` (Microsoft Office if installed, else LibreOffice), `msoffice`, `libreoffice`, `none` |
| `libreoffice_path` | `""` | full path to `soffice.exe` if LibreOffice is not in `C:\Program Files\LibreOffice\program\` |
| `office_timeout_seconds` | `120` | maximum time for one Word/Excel conversion (the converter is killed after it) |

Everything else (defaults, limits, retention, log level, login requirement, borderless,
copies mode…) is in **Admin › Settings** and stored in the database.

Data locations: `data\lps.sqlite3` (users, jobs, audit, settings), `data\secret_key`
(session signing key), `data\uploads`, `data\jobs` (temporary files, cleaned automatically),
`logs\app.log` (rotating, 5 × 5 MB), `logs\service-*.log` (NSSM).

## 6a. Word / Excel support (.docx .doc .xlsx .xls)

Word and Excel files are **converted to PDF on the server when they are uploaded**; the PDF
then goes through the normal pipeline (page selection, N-up, settings, preview, Epson
printing). Office files are never sent to the printer driver. One of these must be installed
on the print-server PC:

| Option | Install | Notes |
|---|---|---|
| **LibreOffice** (recommended for the service) | Free download from libreoffice.org, default location | Runs headless with a throw-away profile; reliable inside a Windows service. Layout is very close to Word/Excel. |
| **Microsoft Office** (Word + Excel) | Licensed, activated desktop Office | Best layout fidelity. Microsoft does not officially support Office automation inside a service; confirm with **Test conversion**. The installer creates the `systemprofile\Desktop` folders Office needs under LocalSystem. Open Word and Excel once interactively after installing to complete first-run/activation prompts. |

With `"office_converter": "auto"` the app uses Microsoft Office when Word/Excel are
installed, otherwise LibreOffice. To force one, set `"office_converter": "libreoffice"` or
`"msoffice"` in `config.json` and restart the service (`nssm restart LocalPrintServer`).

After installing, open **Admin › Printer › Word / Excel conversion** and press **Test
conversion**. It converts a built-in sample document and workbook (nothing is printed) and
shows the engine, the time and the page count. If Word/Excel is not available, users see
"Word/Excel printing is not available on this server right now…" and can still print PDFs.

Fonts: install on the server any special fonts your documents use (otherwise the converter
substitutes them, which can move line and page breaks).

No new Python packages are needed for Word/Excel (pywin32 is already required).

## 7. Backup and rollback

* Every `install.ps1` run creates `C:\LocalPrintServer_backups\LocalPrintServer_<timestamp>\`
  with `files\` (full copy of the previous folder), `manifest.json` (previous NSSM settings) and
  any moved Startup launchers.
* Roll back to the newest backup:
  ```powershell
  powershell -ExecutionPolicy Bypass -File C:\LocalPrintServer\scripts\rollback.ps1
  ```
  or a specific one with `-Backup <folder>`. The replaced version is kept as
  `C:\LocalPrintServer_replaced_<timestamp>` for investigation.
* Routine data backup: stop the service and copy `C:\LocalPrintServer\data\` (or copy
  `lps.sqlite3` together with its `-wal`/`-shm` files).

## 8. Upgrading later

Run `install.ps1` again from the new package. `config.json`, `data\` and `logs\` are kept;
the previous version is backed up first.
