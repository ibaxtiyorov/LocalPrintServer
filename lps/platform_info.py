"""Small Windows process facts used by the admin diagnostics (kept out of the web layer)."""
from __future__ import annotations

import os


def windows_session_id() -> int | None:
    """0 = running as a Windows service (session 0); None = unknown / not Windows."""
    try:
        import win32ts
        return win32ts.ProcessIdToSessionId(os.getpid())
    except Exception:
        return None
