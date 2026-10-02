"""LocalPrintServer administration CLI (run on the server, in the project folder).

  python manage.py create-admin              create an administrator (prompts for password)
  python manage.py reset-password <user>     set a new password (user must change it at login)
  python manage.py enable-user <user>        re-enable a disabled account
  python manage.py list-users
  python manage.py check                     verify Python packages, config, printer (no printing)
"""
from __future__ import annotations

import argparse
import getpass
import importlib
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)
sys.path.insert(0, BASE_DIR)

from lps.audit import AuditLog  # noqa: E402
from lps.auth import UserError, UserService  # noqa: E402
from lps.config import load_config  # noqa: E402
from lps.db import Database  # noqa: E402


def _services():
    cfg = load_config()
    db = Database(cfg.db_path)
    return cfg, UserService(db), AuditLog(db)


def _ask_password() -> str:
    for _ in range(3):
        p1 = getpass.getpass("New password (min 10 chars): ")
        p2 = getpass.getpass("Repeat password: ")
        if p1 == p2:
            return p1
        print("Passwords do not match.")
    sys.exit(2)


def cmd_create_admin(args):
    _, users, audit = _services()
    username = args.username or input("Admin username: ").strip()
    try:
        users.create(username, _ask_password(), "ADMIN", args.display_name or "")
    except UserError as e:
        print(f"Error: {e.code}")
        return 1
    audit.record("cli", "setup.create_admin", username, "OK", ip="local-cli")
    print(f"Administrator '{username}' created.")
    return 0


def cmd_reset_password(args):
    _, users, audit = _services()
    u = users.by_username(args.username)
    if not u:
        print("No such user.")
        return 1
    try:
        users.set_password(u["id"], _ask_password(), must_change=not args.no_force_change)
    except UserError as e:
        print(f"Error: {e.code}")
        return 1
    audit.record("cli", "user.password", u["username"], "OK", ip="local-cli")
    print("Password updated.")
    return 0


def cmd_enable_user(args):
    _, users, audit = _services()
    u = users.by_username(args.username)
    if not u:
        print("No such user.")
        return 1
    users.set_active(u["id"], True)
    audit.record("cli", "user.enable", u["username"], "OK", ip="local-cli")
    print("User enabled.")
    return 0


def cmd_list_users(args):
    _, users, _ = _services()
    for u in users.list():
        print(f"{u['username']:<24} {u['role']:<6} {'active' if u['is_active'] else 'DISABLED':<9}"
              f" last login: {u['last_login_at'] or '-'}")
    return 0


def cmd_check(args):
    ok = True
    print(f"Python {sys.version.split()[0]} at {sys.executable}")
    for mod, pkg in (("flask", "Flask"), ("PIL", "Pillow"), ("win32print", "pywin32"),
                     ("pymupdf", "PyMuPDF"), ("waitress", "waitress")):
        try:
            importlib.import_module(mod)
            print(f"  [ok]      {pkg}")
        except ImportError:
            ok = False
            print(f"  [MISSING] {pkg}   ->  pip install -r requirements.txt")
    cfg = load_config()
    print(f"Config: host={cfg['host']} port={cfg['port']} backend={cfg['printer_backend']}"
          f" printer={cfg['printer_name']!r}")
    print(f"Data dir: {cfg.data_dir}")
    if cfg["printer_backend"] == "win32":
        try:
            from lps.printer.win32_backend import Win32Backend
            b = Win32Backend(cfg["printer_name"], cfg["usb_probe_enabled"], cfg["usb_vendor_id"])
            print(f"  Spooler running: {b.spooler_running()}")
            snap = b.printer_snapshot()
            print(f"  Printer snapshot: {snap}")
            caps = b.capabilities(refresh=True)
            print(f"  Driver paper ids: {len(caps['paper_ids'])}, borderless candidates:"
                  f" {caps['borderless'] or 'none'}")
            print(f"  USB device present (supplementary): {b.usb_present(force=True)}")
            if not snap.get("exists"):
                ok = False
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"  Printer check failed: {e}")
    try:
        from lps.office import OfficeConverter
        st = OfficeConverter(cfg).status()
        print(f"Word/Excel conversion (office_converter={st['setting']}):"
              f" Word -> {st['word_engine'] or 'NOT AVAILABLE (' + st['word_reason'] + ')'},"
              f" Excel -> {st['excel_engine'] or 'NOT AVAILABLE (' + st['excel_reason'] + ')'}")
        if st["libreoffice_path"]:
            print(f"  LibreOffice: {st['libreoffice_path']}")
    except Exception as e:  # noqa: BLE001
        print(f"  Word/Excel converter check failed: {e}")
    _, users, _ = _services()
    if users.count_admins() == 0:
        print("No administrator yet: run  python manage.py create-admin")
    print("RESULT:", "OK" if ok else "PROBLEMS FOUND")
    return 0 if ok else 1


def main():
    p = argparse.ArgumentParser(description="LocalPrintServer administration")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("create-admin")
    a.add_argument("--username")
    a.add_argument("--display-name")
    a.set_defaults(fn=cmd_create_admin)
    r = sub.add_parser("reset-password")
    r.add_argument("username")
    r.add_argument("--no-force-change", action="store_true")
    r.set_defaults(fn=cmd_reset_password)
    e = sub.add_parser("enable-user")
    e.add_argument("username")
    e.set_defaults(fn=cmd_enable_user)
    sub.add_parser("list-users").set_defaults(fn=cmd_list_users)
    sub.add_parser("check").set_defaults(fn=cmd_check)
    args = p.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
