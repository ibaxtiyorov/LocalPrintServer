"""Regression tests for the isolated Microsoft Office COM helper."""
from __future__ import annotations

import sys
import types

from lps import office_helper


class _Documents:
    Count = 0

    def Open(self, **kwargs):
        assert kwargs["PasswordDocument"] == office_helper.DUMMY_PASSWORD
        assert kwargs["OpenAndRepair"] is False
        return None


class _Application:
    Visible = True
    DisplayAlerts = True
    AutomationSecurity = 0
    Options = types.SimpleNamespace()
    Documents = _Documents()

    @property
    def ActiveDocument(self):
        raise RuntimeError("no active document")

    def Quit(self, *_args):
        self.quit_called = True


def test_word_open_none_is_reported_without_none_close(tmp_path, monkeypatch, capsys):
    """A failed COM Open must not be replaced by a misleading None.Close error."""
    app = _Application()
    app.quit_called = False

    pythoncom = types.ModuleType("pythoncom")
    pythoncom.CoInitialize = lambda: None
    pythoncom.CoUninitialize = lambda: None
    client = types.ModuleType("win32com.client")
    client.DispatchEx = lambda _progid: app
    win32com = types.ModuleType("win32com")
    win32com.client = client
    monkeypatch.setitem(sys.modules, "pythoncom", pythoncom)
    monkeypatch.setitem(sys.modules, "win32com", win32com)
    monkeypatch.setitem(sys.modules, "win32com.client", client)
    monkeypatch.setattr(office_helper, "_session_pids", lambda _image: set())

    pid_file = tmp_path / "office.pid"
    result = office_helper.main([
        "office_helper", "word", str(tmp_path / "input.docx"),
        str(tmp_path / "output.pdf"), str(pid_file),
    ])

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert result == 1
    assert "RuntimeError: Word Documents.Open returned None" in output
    assert "documents_count=0" in output
    assert "active_document=unavailable: no active document" in output
    assert "NoneType" not in output
    assert app.quit_called
    assert pid_file.read_text(encoding="ascii") == ""
