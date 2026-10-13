#!/usr/bin/env python3
"""Development preview: the app's page in an ordinary browser, driving the real engine.

    python3 scripts/preview.py [--data FOLDER] [--port 8765] [--today 2026-10-12]

Serves Application Files/index.html on http://127.0.0.1:<port> with a stand-in for
pywebview's bridge that POSTs to /api, which calls the same BudgetService.dispatch()
the app uses. Handy for looking at and clicking through the interface (Claude's
browser pane can't see inside the app's own window).

This is a development tool only: it isn't bundled, and the app itself never opens a
network port. It refuses to use the app's real data folder; by default it uses
~/Library/Caches/I Need A Budget-preview.
"""

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_FILES = ROOT / "Application Files"
REAL_DATA = Path.home() / "Library" / "Application Support" / "I Need A Budget"
SHIM = ("<script>window.pywebview = {api: {"
        "call: (name, args) => fetch('/api', {method: 'POST', headers: {'Content-Type': 'application/json'}, "
        "body: JSON.stringify({name, args})}).then(r => r.json()),"
        "log_error: m => console.error(m)}};</script>\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", default=str(Path.home() / "Library" / "Caches" / "I Need A Budget-preview"))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--today", help="pretend it's this date (YYYY-MM-DD)")
    args = parser.parse_args()
    data = Path(args.data).expanduser().resolve()
    if data == REAL_DATA.resolve():
        sys.exit("The preview never uses the app's real data folder. Pass a throwaway --data folder.")
    if args.today:
        os.environ["INAB_TODAY"] = args.today
    data.mkdir(parents=True, exist_ok=True)

    sys.path.insert(0, str(APP_FILES))
    from db import Database
    from service import BudgetService
    from settings import Settings

    service = BudgetService(Database(data / "budget.db"), Settings(data / "settings.json"), data, hooks={
        "app_info": lambda: {"version": "preview", "data_dir": str(data), "log_path": "", "updates": None},
    })

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body, content_type):
            payload = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            if self.path not in ("/", "/index.html"):
                return self._send(404, "Not found", "text/plain")
            html = (APP_FILES / "index.html").read_text(encoding="utf-8")
            html = html.replace("connect-src 'none'", "connect-src 'self'")
            html = html.replace("<script>\n'use strict';", SHIM + "<script>\n'use strict';", 1)
            self._send(200, html, "text/html; charset=utf-8")

        def do_POST(self):
            if self.path != "/api":
                return self._send(404, "Not found", "text/plain")
            request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            result = service.dispatch(str(request.get("name")), request.get("args"))
            self._send(200, json.dumps(result), "application/json")

        def log_message(self, *args):
            pass

    print(f"Preview on http://127.0.0.1:{args.port} (data: {data})", flush=True)
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
