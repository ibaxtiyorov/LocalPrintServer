"""LocalPrintServer entry point.

NSSM runs:  python.exe app.py   (startup directory C:\\LocalPrintServer)
Serves the LAN web UI on host/port from config.json (default 0.0.0.0:5000)
using the waitress production WSGI server.
"""
from __future__ import annotations

import logging
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from lps import create_app  # noqa: E402
from lps.config import load_config  # noqa: E402

log = logging.getLogger("lps.main")


def main() -> int:
    cfg = load_config()
    app = create_app(cfg)
    manager = app.extensions["lps"].manager
    host, port = cfg["host"], int(cfg["port"])
    log.info("Starting web server on http://%s:%d/", host, port)
    try:
        try:
            from waitress import serve
        except ImportError:
            log.warning("waitress is not installed - falling back to the Flask development server")
            app.run(host=host, port=port, threaded=True, use_reloader=False)
        else:
            serve(app, host=host, port=port, threads=int(cfg["threads"]), ident="LocalPrintServer",
                  max_request_body_size=(int(cfg["hard_max_upload_mb"]) + 1) * 1024 * 1024,
                  channel_timeout=120, clear_untrusted_proxy_headers=True)
    except KeyboardInterrupt:
        log.info("Stop requested")
    except OSError:
        log.exception("Web server could not start (is port %d already in use?)", port)
        return 1
    finally:
        manager.stop()
        log.info("LocalPrintServer stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
