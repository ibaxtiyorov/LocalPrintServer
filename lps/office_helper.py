"""Converts ONE Word or Excel file to PDF with Microsoft Office COM automation.

Started by lps.office as a separate process:
    python -s -m lps.office_helper word|excel <input> <output.pdf> <pid-file>
so that a hung Office instance can be killed (the PIDs of the Office process it
starts are written to <pid-file>) without affecting the web service.

Office is started invisibly with DispatchEx (a private instance, never the user's
open Word/Excel), macros force-disabled, alerts off, files opened read-only and
never saved. The output is a PDF; nothing is sent to any printer.
"""
from __future__ import annotations

import csv
import io
import os
import subprocess
import sys

MSO_AUTOMATION_SECURITY_FORCE_DISABLE = 3
WD_EXPORT_FORMAT_PDF = 17
WD_EXPORT_OPTIMIZE_FOR_PRINT = 0
WD_DO_NOT_SAVE_CHANGES = 0
WD_ALERTS_NONE = 0
XL_TYPE_PDF = 0
XL_QUALITY_STANDARD = 0
DUMMY_PASSWORD = "lps-no-password"      # makes protected files fail instead of prompting


def _session_pids(image: str) -> set[int]:
    """PIDs of `image` running in this process's Windows session."""
    try:
        import win32ts
        my_session = win32ts.ProcessIdToSessionId(os.getpid())
    except Exception:
        my_session = None
    out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {image}", "/FO", "CSV", "/NH"],
                         capture_output=True, text=True,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    pids = set()
    for row in csv.reader(io.StringIO(out)):
        if len(row) >= 4 and row[0].lower() == image.lower() and row[1].isdigit():
            if my_session is None or (row[3].isdigit() and int(row[3]) == my_session):
                pids.add(int(row[1]))
    return pids


def _set(obj, name, value):
    try:
        setattr(obj, name, value)
    except Exception:
        pass


def convert_word(app_obj, inp: str, out: str):
    _set(app_obj, "Visible", False)
    _set(app_obj, "DisplayAlerts", WD_ALERTS_NONE)
    _set(app_obj, "AutomationSecurity", MSO_AUTOMATION_SECURITY_FORCE_DISABLE)
    try:
        _set(app_obj.Options, "ConfirmConversions", False)
        _set(app_obj.Options, "UpdateLinksAtOpen", False)
        _set(app_obj.Options, "SaveNormalPrompt", False)
    except Exception:
        pass
    doc = app_obj.Documents.Open(FileName=inp, ConfirmConversions=False, ReadOnly=True,
                                 AddToRecentFiles=False, PasswordDocument=DUMMY_PASSWORD,
                                 Revert=False, Visible=False, OpenAndRepair=False,
                                 NoEncodingDialog=True)
    try:
        doc.ExportAsFixedFormat(OutputFileName=out, ExportFormat=WD_EXPORT_FORMAT_PDF,
                                OpenAfterExport=False, OptimizeFor=WD_EXPORT_OPTIMIZE_FOR_PRINT,
                                IncludeDocProps=False, DocStructureTags=False,
                                BitmapMissingFonts=True, UseISO19005_1=False)
    finally:
        doc.Close(SaveChanges=WD_DO_NOT_SAVE_CHANGES)


def convert_excel(app_obj, inp: str, out: str):
    _set(app_obj, "Visible", False)
    _set(app_obj, "DisplayAlerts", False)
    _set(app_obj, "AutomationSecurity", MSO_AUTOMATION_SECURITY_FORCE_DISABLE)
    _set(app_obj, "AskToUpdateLinks", False)
    _set(app_obj, "EnableEvents", False)
    _set(app_obj, "ScreenUpdating", False)
    wb = app_obj.Workbooks.Open(Filename=inp, UpdateLinks=0, ReadOnly=True,
                                Password=DUMMY_PASSWORD, IgnoreReadOnlyRecommended=True,
                                Notify=False, AddToMru=False)
    try:
        wb.ExportAsFixedFormat(Type=XL_TYPE_PDF, Filename=out, Quality=XL_QUALITY_STANDARD,
                               IncludeDocProperties=False, IgnorePrintAreas=False,
                               OpenAfterPublish=False)
    finally:
        wb.Close(SaveChanges=False)


def main(argv: list[str]) -> int:
    if len(argv) != 5 or argv[1] not in ("word", "excel"):
        print("usage: office_helper word|excel <input> <output.pdf> <pid-file>")
        return 2
    kind, inp, out, pid_file = argv[1], os.path.abspath(argv[2]), os.path.abspath(argv[3]), argv[4]
    image, progid = (("WINWORD.EXE", "Word.Application") if kind == "word"
                     else ("EXCEL.EXE", "Excel.Application"))
    import pythoncom
    import win32com.client
    pythoncom.CoInitialize()
    before = _session_pids(image)
    app_obj = None
    try:
        app_obj = win32com.client.DispatchEx(progid)
        started = _session_pids(image) - before
        with open(pid_file, "w", encoding="ascii") as fh:
            fh.write(" ".join(str(p) for p in sorted(started)))
        (convert_word if kind == "word" else convert_excel)(app_obj, inp, out)
        return 0 if os.path.exists(out) else 3
    except Exception as e:      # message goes to the server log via the parent process
        print(f"{type(e).__name__}: {e}")
        return 1
    finally:
        if app_obj is not None:
            try:
                if kind == "word":
                    app_obj.Quit(WD_DO_NOT_SAVE_CHANGES)    # never prompt to save Normal.dotm
                else:
                    app_obj.Quit()
            except Exception:
                pass
        app_obj = None
        pythoncom.CoUninitialize()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
